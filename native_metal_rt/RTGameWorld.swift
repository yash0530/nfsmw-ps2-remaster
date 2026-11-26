import Foundation
import simd

// MARK: - Road Network PODs (1:1 Binary Match with .rkpt v2 WRoadNetwork #22)

struct RTRoadNodeSpec {
    var pos: SIMD3<Float>
    var nodeIdx: UInt16
    var flags: UInt16
    var degree: UInt8
    var adjSegs: [UInt16] // up to 7 adjacent segment indices
}

struct RTRoadSegmentSpec {
    var node0: UInt16
    var node1: UInt16
    var laneFlags: UInt8
    var dirFlags: UInt8
    var roadId: UInt16
    var lengthM: Float
    var halfWidthM: Float
}

/// Fast 2D XZ spatial index over Rockport's 4,385 road nodes & 6,538 road segments.
final class RTRoadGraph {
    let nodes: [RTRoadNodeSpec]
    let segments: [RTRoadSegmentSpec]
    let boundsMinXZ: SIMD2<Float>
    let boundsMaxXZ: SIMD2<Float>

    private let gridDim: Int = 64
    private let cellSizeXZ: SIMD2<Float>
    private var cellSegments: [[Int]]

    var isEmpty: Bool { nodes.isEmpty || segments.isEmpty }

    init(nodes: [RTRoadNodeSpec], segments: [RTRoadSegmentSpec]) {
        self.nodes = nodes
        self.segments = segments

        var minX: Float = 1e9, minZ: Float = 1e9
        var maxX: Float = -1e9, maxZ: Float = -1e9
        for n in nodes {
            if n.pos.x < minX { minX = n.pos.x }
            if n.pos.z < minZ { minZ = n.pos.z }
            if n.pos.x > maxX { maxX = n.pos.x }
            if n.pos.z > maxZ { maxZ = n.pos.z }
        }
        if nodes.isEmpty {
            minX = -500; minZ = -500; maxX = 500; maxZ = 500
        }
        self.boundsMinXZ = SIMD2<Float>(minX - 20, minZ - 20)
        self.boundsMaxXZ = SIMD2<Float>(maxX + 20, maxZ + 20)
        let spanX = max(100.0, self.boundsMaxXZ.x - self.boundsMinXZ.x)
        let spanZ = max(100.0, self.boundsMaxXZ.y - self.boundsMinXZ.y)
        self.cellSizeXZ = SIMD2<Float>(spanX / Float(gridDim), spanZ / Float(gridDim))
        self.cellSegments = Array(repeating: [], count: gridDim * gridDim)

        // Rasterize every segment's XZ AABB into overlapping spatial buckets
        for (sIdx, seg) in segments.enumerated() {
            let n0 = Int(seg.node0)
            let n1 = Int(seg.node1)
            guard n0 < nodes.count && n1 < nodes.count else { continue }
            let p0 = nodes[n0].pos
            let p1 = nodes[n1].pos
            let cMinX = cellIndexX(min(p0.x, p1.x))
            let cMaxX = cellIndexX(max(p0.x, p1.x))
            let cMinZ = cellIndexZ(min(p0.z, p1.z))
            let cMaxZ = cellIndexZ(max(p0.z, p1.z))
            for cz in cMinZ...cMaxZ {
                let row = cz * gridDim
                for cx in cMinX...cMaxX {
                    cellSegments[row + cx].append(sIdx)
                }
            }
        }
    }

    @inline(__always)
    private func cellIndexX(_ x: Float) -> Int {
        let i = Int((x - boundsMinXZ.x) / cellSizeXZ.x)
        return max(0, min(gridDim - 1, i))
    }

    @inline(__always)
    private func cellIndexZ(_ z: Float) -> Int {
        let i = Int((z - boundsMinXZ.y) / cellSizeXZ.y)
        return max(0, min(gridDim - 1, i))
    }

    /// Queries deduplicated segment indices whose bounding cells overlap `[posXZ - radius .. posXZ + radius]`.
    func querySegments(near posXZ: SIMD2<Float>, radius: Float) -> [Int] {
        guard !isEmpty else { return [] }
        let cMinX = cellIndexX(posXZ.x - radius)
        let cMaxX = cellIndexX(posXZ.x + radius)
        let cMinZ = cellIndexZ(posXZ.y - radius)
        let cMaxZ = cellIndexZ(posXZ.y + radius)

        var seen = Set<Int>()
        var result: [Int] = []
        result.reserveCapacity(128)
        for cz in cMinZ...cMaxZ {
            let row = cz * gridDim
            for cx in cMinX...cMaxX {
                for sIdx in cellSegments[row + cx] {
                    if seen.insert(sIdx).inserted {
                        result.append(sIdx)
                    }
                }
            }
        }
        return result
    }

    /// Finds the nearest road segment & normalized parameter `t in [0..1]` to `worldPos`.
    func nearestSegment(to worldPos: SIMD3<Float>, searchRadius: Float = 160.0) -> (segIdx: Int, t: Float, point: SIMD3<Float>, distSq: Float)? {
        let cands = querySegments(near: SIMD2<Float>(worldPos.x, worldPos.z), radius: searchRadius)
        var bestIdx = -1
        var bestT: Float = 0.0
        var bestPt = worldPos
        var bestDistSq: Float = .greatestFiniteMagnitude

        for sIdx in cands {
            let seg = segments[sIdx]
            let p0 = nodes[Int(seg.node0)].pos
            let p1 = nodes[Int(seg.node1)].pos
            let ab = SIMD2<Float>(p1.x - p0.x, p1.z - p0.z)
            let ap = SIMD2<Float>(worldPos.x - p0.x, worldPos.z - p0.z)
            let lenSq = max(1e-4, simd_dot(ab, ab))
            let t = max(0.0, min(1.0, simd_dot(ap, ab) / lenSq))
            let pt = p0 + (p1 - p0) * t
            let dx = pt.x - worldPos.x
            let dz = pt.z - worldPos.z
            let dSq = dx * dx + dz * dz
            if dSq < bestDistSq {
                bestDistSq = dSq
                bestIdx = sIdx
                bestT = t
                bestPt = pt
            }
        }
        guard bestIdx >= 0 else { return nil }
        return (bestIdx, bestT, bestPt, bestDistSq)
    }

    /// Standalone binary reader for .rkpt v2 WRoadNetwork trailing tables (matches #22 locked ABI).
    static func loadFromRkpt(url: URL) throws -> RTRoadGraph {
        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
        guard data.count >= 256 else {
            return RTRoadGraph(nodes: [], segments: [])
        }
        return data.withUnsafeBytes { rawBuf -> RTRoadGraph in
            guard let base = rawBuf.baseAddress else { return RTRoadGraph(nodes: [], segments: []) }
            func rU16(_ o: Int) -> UInt16 { var v: UInt16 = 0; memcpy(&v, base.advanced(by: o), 2); return UInt16(littleEndian: v) }
            func rU32(_ o: Int) -> UInt32 { var v: UInt32 = 0; memcpy(&v, base.advanced(by: o), 4); return UInt32(littleEndian: v) }
            func rU64(_ o: Int) -> UInt64 { var v: UInt64 = 0; memcpy(&v, base.advanced(by: o), 8); return UInt64(littleEndian: v) }
            func rF32(_ o: Int) -> Float { Float(bitPattern: rU32(o)) }
            func rU8(_ o: Int) -> UInt8 { base.advanced(by: o).assumingMemoryBound(to: UInt8.self).pointee }

            let version = rU32(4)
            let flags = rU32(8)
            guard version >= 2 && (flags & 1) != 0 else {
                return RTRoadGraph(nodes: [], segments: [])
            }

            let nodeCount = Int(rU32(160))
            let segCount = Int(rU32(164))
            let nodesOff = Int(rU64(192))
            let segsOff = Int(rU64(200))
            guard nodeCount > 0, segCount > 0,
                  nodesOff + nodeCount * 32 <= data.count,
                  segsOff + segCount * 16 <= data.count else {
                return RTRoadGraph(nodes: [], segments: [])
            }

            var nodes: [RTRoadNodeSpec] = []
            nodes.reserveCapacity(nodeCount)
            for i in 0..<nodeCount {
                let o = nodesOff + i * 32
                let pos = SIMD3<Float>(rF32(o), rF32(o + 4), rF32(o + 8))
                let idx = rU16(o + 12)
                let flg = rU16(o + 14)
                let deg = rU8(o + 16)
                var adj: [UInt16] = []
                adj.reserveCapacity(7)
                for k in 0..<7 {
                    let s = rU16(o + 18 + k * 2)
                    if s != 0xFFFF { adj.append(s) }
                }
                nodes.append(RTRoadNodeSpec(pos: pos, nodeIdx: idx, flags: flg, degree: deg, adjSegs: adj))
            }

            var segs: [RTRoadSegmentSpec] = []
            segs.reserveCapacity(segCount)
            for i in 0..<segCount {
                let o = segsOff + i * 16
                segs.append(RTRoadSegmentSpec(
                    node0: rU16(o),
                    node1: rU16(o + 2),
                    laneFlags: rU8(o + 4),
                    dirFlags: rU8(o + 5),
                    roadId: rU16(o + 6),
                    lengthM: max(0.5, rF32(o + 8)),
                    halfWidthM: max(2.5, rF32(o + 12))
                ))
            }
            return RTRoadGraph(nodes: nodes, segments: segs)
        }
    }
}

// MARK: - AI Archetypes, Pursuit FSM & Per-Frame Outputs

enum RTAIVehicleArchetype: Int, CaseIterable {
    case copMidsize  = 0  // Heat 1..2 Rockport Civic Cruiser (LOD A)
    case copGTO      = 1  // Heat 3..4 State Pursuit GTO (LOD B)
    case copSport    = 2  // Heat 5 Federal Corvette C6 (LOD B)
    case trafTaxi    = 3  // Traffic Taxi (LOD C)
    case trafSedan   = 4  // Traffic 4-Door Sedan (LOD C)
    case trafSUV     = 5  // Traffic SUV (LOD C)
    case trafMinivan = 6  // Traffic Minivan (LOD C)

    var isPolice: Bool { rawValue <= 2 }

    var manifestKey: String {
        switch self {
        case .copMidsize:  return "copmidsize"
        case .copGTO:      return "copgto"
        case .copSport:    return "copsport"
        case .trafTaxi:    return "traftaxi"
        case .trafSedan:   return "traf4dseda"
        case .trafSUV:     return "trafsuva"
        case .trafMinivan: return "trafminivan"
        }
    }

    /// Local Y-up roof lightbar offset matching build/gameplay_pack/ai_vehicles/manifest.json
    var localLightbarOffset: SIMD3<Float> {
        switch self {
        case .copMidsize: return SIMD3<Float>(0.0, 1.463,  0.073)
        case .copGTO:     return SIMD3<Float>(0.0, 1.393, -0.216)
        case .copSport:   return SIMD3<Float>(0.0, 1.040, -2.090)
        default:          return SIMD3<Float>(0.0, 1.40,   0.0)
        }
    }

    var halfLength: Float {
        switch self {
        case .copMidsize: return 2.55
        case .copGTO:     return 2.40
        case .copSport:   return 2.25
        case .trafSUV, .trafMinivan: return 2.42
        default:          return 2.22
        }
    }

    var halfWidth: Float {
        return isPolice ? 0.96 : 0.90
    }
}

enum RTPursuitPhase: Int {
    case freeRoam = 0
    case patrol   = 1
    case sighted  = 2
    case pursuing = 3
    case cooldown = 4
    case evaded   = 5
    case busted   = 6
}

enum RTCopTactic: Int {
    case patrol = 0
    case rearRam = 1
    case pitLeft = 2
    case pitRight = 3
    case boxInFront = 4
}

/// Per-slot AI vehicle state consumed directly by RTRenderer TLAS slots [player ..< player+32].
struct RTAIInstanceState {
    var slot: Int
    var active: Bool = false
    var archetype: RTAIVehicleArchetype = .trafSedan
    var worldPosition: SIMD3<Float> = .zero
    var prevWorldPosition: SIMD3<Float> = .zero
    var velocityWorld: SIMD3<Float> = .zero
    var yawRad: Float = 0.0
    var prevYawRad: Float = 0.0
    var speedMs: Float = 0.0
    var wheelSpinRad: Float = 0.0
    var steerAngleRad: Float = 0.0
    var isCop: Bool = false
    var sirenOn: Bool = false
    var lightbarPhase: Float = 0.0
    var tactic: RTCopTactic = .patrol

    // Road-following cursor
    var segIdx: Int = -1
    var segT: Float = 0.0
    var segForward: Bool = true
    var cruiseSpeedMs: Float = 16.0
    var laneOffsetM: Float = 2.1
    var stuckTimer: Float = 0.0
    var hasRoadProbeY: Bool = false
}

/// Nearest active police lightbar light ready for RTDynamicFX[13].copLightPos/Col.
struct RTCopLightbarLight {
    var worldPos: SIMD3<Float> = .zero
    var strobePhase: Float = 0.0      // 0 = off, > 0 active phase
    var colorHDR: SIMD3<Float> = .zero
    var intensity: Float = 0.0
}

/// Nearest pursuing cop siren state ready for RTAudioEngine.update(...).
struct RTSirenAudioOutput {
    var gain: Float = 0.0
    var pan: Float = 0.0
    var doppler: Float = 1.0
    var mode: Int32 = 0 // 0=wail, 1=yelp, 2=phaser
    var radarPing: Bool = false
}

/// VLT-loaded heat escalation rule from vlt_vehicles.json `pursuit_levels`.
struct RTHeatLevelConfig {
    var heatLevel: Int
    var copArchetype: RTAIVehicleArchetype
    var maxActiveCops: Int
    var spawnIntervalSec: Float
    var maxCopSpeedMs: Float
    var bountyPerSec: Float
}

// MARK: - Live AI Traffic + Heat 1-5 Police Pursuit Manager

final class RTGameWorld {
    static let maxAISlots = 32
    static let targetTrafficCount = 16
    static let maxActiveCopsCap = 4 // fits cleanly alongside 4 nearest RT cop lightbar slots

    private(set) var roadGraph: RTRoadGraph
    private(set) var aiSlots: [RTAIInstanceState]
    private(set) var copLights: [RTCopLightbarLight] = Array(repeating: RTCopLightbarLight(), count: 4)
    private(set) var sirenAudio = RTSirenAudioOutput()

    // Pursuit State Machine
    private(set) var pursuitPhase: RTPursuitPhase = .freeRoam
    private(set) var heatLevel: Int = 0
    private(set) var bounty: Float = 0.0
    private(set) var evadeProgress: Float = 0.0
    private(set) var bustedProgress: Float = 0.0
    private(set) var activeCopCount: Int = 0
    private(set) var activeTrafficCount: Int = 0
    private(set) var pursuitDurationSec: Float = 0.0
    private(set) var infractionsCount: Int = 0

    // Player-AI collision response for current tick
    private(set) var playerBumpImpulse: SIMD3<Float> = .zero
    private(set) var playerCollisionIntensity: Float = 0.0

    private var heatConfigs: [Int: RTHeatLevelConfig] = [:]
    private var simClock: Float = 0.0
    private var losLostTimer: Float = 0.0
    private var outcomeBannerTimer: Float = 0.0
    private var copSpawnTimer: Float = 0.0
    private var rngState: UInt64 = 0x2005_1115_BEEF_CAFE

    init(roadGraph: RTRoadGraph = RTRoadGraph(nodes: [], segments: []), vltJSONURL: URL? = nil) {
        self.roadGraph = roadGraph
        self.aiSlots = (0..<Self.maxAISlots).map { RTAIInstanceState(slot: $0) }
        loadDefaultHeatConfigs()
        if let url = vltJSONURL ?? Self.defaultVLTURL() {
            loadHeatConfigs(from: url)
        }
    }

    private static func defaultVLTURL() -> URL? {
        for p in ["build/gameplay_pack/vlt_vehicles.json", "../build/gameplay_pack/vlt_vehicles.json"] {
            let u = URL(fileURLWithPath: p)
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        return nil
    }

    /// Replaces the road graph once RTWorldLoader finishes loading .rkpt v2.
    func setRoadGraph(_ graph: RTRoadGraph) {
        self.roadGraph = graph
        for i in 0..<aiSlots.count {
            aiSlots[i].active = false
            aiSlots[i].segIdx = -1
        }
    }

    /// Convenience adapter to ingest raw arrays from RTWorldLoader without tight module coupling.
    func setRoadGraph(nodes: [RTRoadNodeSpec], segments: [RTRoadSegmentSpec]) {
        setRoadGraph(RTRoadGraph(nodes: nodes, segments: segments))
    }

    private func loadDefaultHeatConfigs() {
        heatConfigs = [
            1: RTHeatLevelConfig(heatLevel: 1, copArchetype: .copMidsize, maxActiveCops: 2, spawnIntervalSec: 8.0, maxCopSpeedMs: 56.0, bountyPerSec: 120),
            2: RTHeatLevelConfig(heatLevel: 2, copArchetype: .copMidsize, maxActiveCops: 3, spawnIntervalSec: 7.5, maxCopSpeedMs: 64.0, bountyPerSec: 250),
            3: RTHeatLevelConfig(heatLevel: 3, copArchetype: .copGTO,     maxActiveCops: 4, spawnIntervalSec: 5.0, maxCopSpeedMs: 74.0, bountyPerSec: 500),
            4: RTHeatLevelConfig(heatLevel: 4, copArchetype: .copGTO,     maxActiveCops: 4, spawnIntervalSec: 4.5, maxCopSpeedMs: 80.0, bountyPerSec: 900),
            5: RTHeatLevelConfig(heatLevel: 5, copArchetype: .copSport,   maxActiveCops: 4, spawnIntervalSec: 3.0, maxCopSpeedMs: 89.0, bountyPerSec: 1600),
        ]
    }

    private func loadHeatConfigs(from url: URL) {
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let pl = root["pursuit_levels"] as? [String: [String: Any]] else { return }

        for lvl in 1...5 {
            guard let rec = pl["heat_0\(lvl)"] else { continue }
            let spawnT = (rec["time_between_cop_spawn"] as? NSNumber)?.floatValue ?? 6.0
            let hwyMph = (rec["search_mode_hwy_mph"] as? NSNumber)?.floatValue ?? 100.0
            let arch: RTAIVehicleArchetype = (lvl <= 2) ? .copMidsize : ((lvl <= 4) ? .copGTO : .copSport)
            let cap = min(Self.maxActiveCopsCap, max(2, lvl <= 2 ? lvl + 1 : 4))
            let maxSpd = max(54.0, (hwyMph * 0.44704) + Float(lvl) * 8.5)
            heatConfigs[lvl] = RTHeatLevelConfig(
                heatLevel: lvl,
                copArchetype: arch,
                maxActiveCops: cap,
                spawnIntervalSec: max(2.0, spawnT),
                maxCopSpeedMs: maxSpd,
                bountyPerSec: Float(120 * (1 << (lvl - 1)))
            )
        }
    }

    /// Instant-thrill pursuit trigger (`F` key): starts Heat 1 or escalates Heat 1->2->3->4->5!
    func triggerInstantPursuit(
        near playerPos: SIMD3<Float>,
        playerYaw: Float,
        setHeat explicitHeat: Int? = nil,
        showcaseFraming: Bool = false
    ) {
        if let h = explicitHeat {
            heatLevel = max(1, min(5, h))
        } else if pursuitPhase == .pursuing || pursuitPhase == .cooldown {
            heatLevel = min(5, max(1, heatLevel + 1))
        } else {
            heatLevel = 1
            bounty = max(bounty, 1500.0)
            pursuitDurationSec = 0.0
            infractionsCount += 1
        }
        if bounty < 1500.0 {
            bounty = Float(heatLevel) * 2500.0
        }
        pursuitPhase = .pursuing
        evadeProgress = 0.0
        bustedProgress = 0.0
        losLostTimer = 0.0
        outcomeBannerTimer = 0.0
        sirenAudio.radarPing = true

        // Immediately upgrade & spawn pursuing units (including close ahead/flank interceptors for instant action!)
        let cfg = heatConfigs[heatLevel] ?? heatConfigs[1]!
        let tactics: [RTCopTactic] = [.boxInFront, .pitLeft, .pitRight, .rearRam]
        let pFwd = SIMD3<Float>(sin(playerYaw), 0.0, cos(playerYaw))
        let pRight = SIMD3<Float>(-cos(playerYaw), 0.0, sin(playerYaw))

        for slotIdx in 0..<Self.maxActiveCopsCap {
            let activeWanted = slotIdx < cfg.maxActiveCops
            if !activeWanted {
                aiSlots[slotIdx].active = false
                continue
            }
            if showcaseFraming || !aiSlots[slotIdx].active || aiSlots[slotIdx].archetype != cfg.copArchetype {
                var placedPos: SIMD3<Float>? = nil
                var placedYaw: Float = playerYaw
                var placedSeg: Int = -1
                var placedT: Float = 0.5
                var placedFwd: Bool = true

                if showcaseFraming {
                    // Place lead & flanking units directly along the road corridor ahead/beside player
                    // so chase-cam pursuit shots visibly show active cop cars + flashing RT lightbars!
                    let forwardOffsets: [Float] = [34.0, 21.0, -14.0, 56.0]
                    let lateralOffsets: [Float] = [-2.8, 3.2, -2.5, 2.2]
                    let desired = playerPos + pFwd * forwardOffsets[slotIdx] + pRight * lateralOffsets[slotIdx]
                    if let near = roadGraph.nearestSegment(to: desired, searchRadius: 90.0) {
                        let seg = roadGraph.segments[near.segIdx]
                        let a = roadGraph.nodes[Int(seg.node0)].pos
                        let b = roadGraph.nodes[Int(seg.node1)].pos
                        let segDir = simd_normalize(b - a)
                        let align = segDir.x * pFwd.x + segDir.z * pFwd.z
                        let useFwd = (align >= 0.0)
                        let travel = useFwd ? segDir : -segDir
                        let segYaw = atan2(travel.x, travel.z)
                        let segRight = SIMD3<Float>(-cos(segYaw), 0.0, sin(segYaw))
                        let safeHalfW = max(1.45, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.62))
                        let laneBias: Float = ((slotIdx % 2 == 0) ? 1.0 : -1.0) * min(1.85, safeHalfW * 0.85)
                        placedPos = SIMD3<Float>(near.point.x + segRight.x * laneBias,
                                                 near.point.y,
                                                 near.point.z + segRight.z * laneBias)
                        placedYaw = segYaw
                        placedSeg = near.segIdx
                        placedT = near.t
                        placedFwd = useFwd
                    } else {
                        placedPos = desired
                        placedYaw = playerYaw
                    }
                }

                if placedPos == nil {
                    let preferAhead = (slotIdx == 0)
                    let spawnDist: Float = 38.0 + Float(slotIdx) * 24.0
                    if let placed = spawnUnitOnRoad(
                        near: playerPos,
                        playerYaw: playerYaw,
                        minDist: spawnDist * 0.70,
                        maxDist: spawnDist * 1.45,
                        preferBehind: !preferAhead,
                        preferAhead: preferAhead
                    ) {
                        placedPos = placed.pos
                        placedYaw = placed.yaw
                        placedSeg = placed.segIdx
                        placedT = placed.t
                        placedFwd = placed.forward
                    }
                }

                if let pos = placedPos {
                    var u = aiSlots[slotIdx]
                    u.active = true
                    u.isCop = true
                    u.archetype = cfg.copArchetype
                    u.worldPosition = pos
                    u.prevWorldPosition = pos
                    u.yawRad = placedYaw
                    u.prevYawRad = placedYaw
                    u.segIdx = placedSeg
                    u.segT = placedT
                    u.segForward = placedFwd
                    u.speedMs = showcaseFraming ? 36.0 : 26.0
                    u.sirenOn = true
                    u.lightbarPhase = Float(slotIdx) * 1.57
                    u.tactic = tactics[slotIdx % tactics.count]
                    u.hasRoadProbeY = false
                    aiSlots[slotIdx] = u
                }
            } else {
                aiSlots[slotIdx].sirenOn = true
                aiSlots[slotIdx].tactic = tactics[slotIdx % tactics.count]
            }
        }
    }

    /// Resets distant traffic & police units around a newly teleported landmark and seeds nearby ahead traffic.
    func resetNearPlayer(playerPos: SIMD3<Float>, playerYaw: Float, keepPursuit: Bool = false) {
        if !keepPursuit {
            clearPursuit()
        }
        for i in 0..<aiSlots.count {
            aiSlots[i].active = false
            aiSlots[i].segIdx = -1
        }
        guard !roadGraph.isEmpty else { return }

        // Seed 3 nearby ahead traffic units so Rockport streets immediately feel alive on frame 0
        let trafficStart = Self.maxActiveCopsCap
        let trafficArchetypes: [RTAIVehicleArchetype] = [.trafTaxi, .trafSedan, .trafSUV, .trafMinivan]
        let pFwd = SIMD3<Float>(sin(playerYaw), 0.0, cos(playerYaw))
        for k in 0..<3 {
            let slot = trafficStart + k
            let probePt = playerPos + pFwd * (32.0 + Float(k) * 34.0)
            if let near = roadGraph.nearestSegment(to: probePt, searchRadius: 70.0) {
                let seg = roadGraph.segments[near.segIdx]
                let a = roadGraph.nodes[Int(seg.node0)].pos
                let b = roadGraph.nodes[Int(seg.node1)].pos
                let segDir = simd_normalize(b - a)
                let useFwd = (k != 1) ? ((segDir.x * pFwd.x + segDir.z * pFwd.z) >= 0.0) : ((segDir.x * pFwd.x + segDir.z * pFwd.z) < 0.0)
                let travel = useFwd ? segDir : -segDir
                let segYaw = atan2(travel.x, travel.z)
                let right = SIMD3<Float>(-cos(segYaw), 0.0, sin(segYaw))
                let safeHalfW = max(1.30, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.55))
                let laneOff = min(1.95, safeHalfW)
                let pos = near.point + right * laneOff
                if simd_distance(SIMD2<Float>(pos.x, pos.z), SIMD2<Float>(playerPos.x, playerPos.z)) >= 16.0 {
                    var u = aiSlots[slot]
                    u.active = true
                    u.isCop = false
                    u.sirenOn = false
                    u.archetype = trafficArchetypes[k % trafficArchetypes.count]
                    u.worldPosition = pos
                    u.prevWorldPosition = pos
                    u.yawRad = segYaw
                    u.prevYawRad = segYaw
                    u.segIdx = near.segIdx
                    u.segT = near.t
                    u.segForward = useFwd
                    u.cruiseSpeedMs = 15.5 + Float(k) * 2.5
                    u.speedMs = u.cruiseSpeedMs
                    u.laneOffsetM = laneOff
                    u.hasRoadProbeY = false
                    aiSlots[slot] = u
                }
            }
        }
        maintainTrafficPopulation(playerPos: playerPos, playerYaw: playerYaw)
        if keepPursuit && heatLevel > 0 {
            triggerInstantPursuit(near: playerPos, playerYaw: playerYaw, setHeat: heatLevel, showcaseFraming: false)
        }
    }

    func clearPursuit() {
        pursuitPhase = .freeRoam
        heatLevel = 0
        evadeProgress = 0.0
        bustedProgress = 0.0
        losLostTimer = 0.0
        outcomeBannerTimer = 0.0
        for i in 0..<Self.maxActiveCopsCap {
            aiSlots[i].sirenOn = false
            aiSlots[i].tactic = .patrol
        }
    }

    /// Ingests GPU road-height probe hits (`heights[i]` corresponds to `aiSlots[i]`, `-9999` on miss).
    func applyRoadProbeHeights(_ heights: [Float], dt: Float, snapImmediate: Bool = false) {
        let count = min(heights.count, aiSlots.count)
        let alpha: Float = snapImmediate ? 1.0 : min(1.0, max(0.15, dt * 36.0))
        for i in 0..<count where aiSlots[i].active {
            let hy = heights[i]
            if hy > -5000.0 && abs(hy - aiSlots[i].worldPosition.y) < 18.0 {
                aiSlots[i].worldPosition.y += (hy - aiSlots[i].worldPosition.y) * alpha
                if !snapImmediate {
                    aiSlots[i].worldPosition.y = max(hy - 0.02, min(hy + 0.02, aiSlots[i].worldPosition.y))
                } else {
                    aiSlots[i].prevWorldPosition.y = aiSlots[i].worldPosition.y
                }
                aiSlots[i].hasRoadProbeY = true
            }
        }
    }

    // MARK: - Main Per-Frame Simulation Step

    func step(
        dt rawDt: Float,
        playerPos: SIMD3<Float>,
        playerVel: SIMD3<Float>,
        playerYaw: Float
    ) {
        let dt = max(0.0005, min(0.05, rawDt))
        simClock += dt
        playerBumpImpulse = .zero
        playerCollisionIntensity = 0.0
        sirenAudio.radarPing = false

        guard !roadGraph.isEmpty else { return }

        let playerSpeedMs = hypot(playerVel.x, playerVel.z)
        let playerSpeedKmh = playerSpeedMs * 3.6

        // 1. Maintain 16 Active Civilian Traffic Units (slots 4 ..< 20)
        maintainTrafficPopulation(playerPos: playerPos, playerYaw: playerYaw)

        // 2. Maintain Patrol / Pursuit Police Units (slots 0 ..< 4)
        maintainPolicePopulation(dt: dt, playerPos: playerPos, playerYaw: playerYaw)

        // 3. Advance Traffic & Police AI Steering/Movement + Player Collisions
        var nTraffic = 0
        var nCops = 0
        var nearestCopDist: Float = 1e9
        var anyCopHasLOS = false

        for i in 0..<Self.maxAISlots {
            guard aiSlots[i].active else { continue }
            aiSlots[i].prevWorldPosition = aiSlots[i].worldPosition
            aiSlots[i].prevYawRad = aiSlots[i].yawRad

            if aiSlots[i].isCop {
                stepPoliceUnit(index: i, dt: dt, playerPos: playerPos, playerVel: playerVel, playerSpeedMs: playerSpeedMs)
                let d = simd_distance(SIMD2<Float>(aiSlots[i].worldPosition.x, aiSlots[i].worldPosition.z),
                                      SIMD2<Float>(playerPos.x, playerPos.z))
                if d < nearestCopDist { nearestCopDist = d }
                if d < 135.0 { anyCopHasLOS = true }
                if aiSlots[i].sirenOn { nCops += 1 }
            } else {
                stepTrafficUnit(index: i, dt: dt, playerPos: playerPos)
                nTraffic += 1
            }

            // Player <-> AI Bounding-Cylinder Arcade Collision
            resolvePlayerCollision(slotIdx: i, playerPos: playerPos, playerVel: playerVel)
        }
        activeTrafficCount = nTraffic
        activeCopCount = nCops

        // 4. Update Heat 1-5 Pursuit State Machine (Patrol -> Sighted -> Pursuing -> Cooldown -> Evaded/Busted)
        stepPursuitFSM(
            dt: dt,
            playerPos: playerPos,
            playerYaw: playerYaw,
            playerSpeedKmh: playerSpeedKmh,
            nearestCopDist: nearestCopDist,
            anyCopHasLOS: anyCopHasLOS
        )

        // 5. Populate Top 4 Nearest Cop RT Lightbars & 3D Doppler Siren Audio
        updateCopLightbarsAndSiren(playerPos: playerPos, playerVel: playerVel, playerYaw: playerYaw)
    }

    // MARK: - Traffic Spawning & Road-Graph Navigation

    private func maintainTrafficPopulation(playerPos: SIMD3<Float>, playerYaw: Float) {
        let trafficStart = Self.maxActiveCopsCap
        let trafficEnd = min(Self.maxAISlots, trafficStart + Self.targetTrafficCount)
        let trafficArchetypes: [RTAIVehicleArchetype] = [.trafTaxi, .trafSedan, .trafSUV, .trafMinivan]

        for slot in trafficStart..<trafficEnd {
            if aiSlots[slot].active {
                let d = simd_distance(SIMD2<Float>(aiSlots[slot].worldPosition.x, aiSlots[slot].worldPosition.z),
                                      SIMD2<Float>(playerPos.x, playerPos.z))
                if d > 285.0 {
                    aiSlots[slot].active = false
                }
            }
            if !aiSlots[slot].active {
                if let placed = spawnUnitOnRoad(near: playerPos, playerYaw: playerYaw, minDist: 55.0, maxDist: 255.0, preferBehind: false) {
                    // Reject spawning right on top of another active AI unit
                    var clear = true
                    for other in 0..<Self.maxAISlots where aiSlots[other].active {
                        if simd_distance(placed.pos, aiSlots[other].worldPosition) < 14.0 {
                            clear = false
                            break
                        }
                    }
                    if clear {
                        let arch = trafficArchetypes[Int(nextRand() % UInt64(trafficArchetypes.count))]
                        let seg = roadGraph.segments[placed.segIdx]
                        let isHighway = seg.halfWidthM >= 6.5
                        let spdKmh: Float = isHighway ? (70.0 + nextRandFloat() * 20.0) : (45.0 + nextRandFloat() * 22.0)
                        var u = aiSlots[slot]
                        u.active = true
                        u.isCop = false
                        u.sirenOn = false
                        u.archetype = arch
                        u.worldPosition = placed.pos
                        u.prevWorldPosition = placed.pos
                        u.yawRad = placed.yaw
                        u.prevYawRad = placed.yaw
                        u.segIdx = placed.segIdx
                        u.segT = placed.t
                        u.segForward = placed.forward
                        u.cruiseSpeedMs = spdKmh / 3.6
                        u.speedMs = u.cruiseSpeedMs
                        let safeHalfW = max(1.30, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.55))
                        u.laneOffsetM = min(1.95, safeHalfW)
                        u.hasRoadProbeY = false
                        aiSlots[slot] = u
                    }
                }
            }
        }
    }

    private func stepTrafficUnit(index: Int, dt: Float, playerPos: SIMD3<Float>) {
        var u = aiSlots[index]
        guard u.segIdx >= 0 && u.segIdx < roadGraph.segments.count else {
            u.active = false
            aiSlots[index] = u
            return
        }

        // Check forward clearance against player and nearby traffic to prevent pile-ups
        let fwd = SIMD3<Float>(sin(u.yawRad), 0, cos(u.yawRad))
        var targetSpeed = u.cruiseSpeedMs

        let toPlayer = playerPos - u.worldPosition
        let pDist = hypot(toPlayer.x, toPlayer.z)
        if pDist < 22.0 && pDist > 0.5 {
            let along = (toPlayer.x * fwd.x + toPlayer.z * fwd.z) / pDist
            if along > 0.72 {
                targetSpeed = (pDist < 10.0) ? 0.0 : u.cruiseSpeedMs * 0.35
            }
        }

        for j in 0..<Self.maxAISlots where j != index && aiSlots[j].active {
            let delta = aiSlots[j].worldPosition - u.worldPosition
            let d = hypot(delta.x, delta.z)
            if d < 18.0 && d > 0.5 {
                let along = (delta.x * fwd.x + delta.z * fwd.z) / d
                if along > 0.78 {
                    targetSpeed = min(targetSpeed, d < 9.0 ? 0.0 : aiSlots[j].speedMs * 0.8)
                }
            }
        }

        let accelRate: Float = (targetSpeed < u.speedMs) ? 14.0 : 5.5
        u.speedMs += (targetSpeed - u.speedMs) * min(1.0, dt * accelRate)
        advanceAlongRoadGraph(&u, dt: dt)
        u.wheelSpinRad += (u.speedMs / 0.31) * dt
        aiSlots[index] = u
    }

    private func advanceAlongRoadGraph(_ u: inout RTAIInstanceState, dt: Float) {
        let seg = roadGraph.segments[u.segIdx]
        let p0 = roadGraph.nodes[Int(seg.node0)].pos
        let p1 = roadGraph.nodes[Int(seg.node1)].pos
        let stepT = (u.speedMs * dt) / max(1.0, seg.lengthM)
        u.segT += u.segForward ? stepT : -stepT

        // Junction transition when reaching either end of segment
        if u.segT >= 1.0 || u.segT <= 0.0 {
            let arrivedNodeIdx = Int(u.segT >= 1.0 ? seg.node1 : seg.node0)
            let currDir = simd_normalize(u.segForward ? (p1 - p0) : (p0 - p1))
            let arrivedNode = roadGraph.nodes[arrivedNodeIdx]

            var bestNextSeg = -1
            var bestForward = true
            var bestScore: Float = -10.0

            for candSegU16 in arrivedNode.adjSegs {
                let candIdx = Int(candSegU16)
                if candIdx == u.segIdx || candIdx >= roadGraph.segments.count { continue }
                let cs = roadGraph.segments[candIdx]
                let isFwd = (Int(cs.node0) == arrivedNodeIdx)
                let otherNode = Int(isFwd ? cs.node1 : cs.node0)
                guard otherNode < roadGraph.nodes.count else { continue }
                let nextDir = simd_normalize(roadGraph.nodes[otherNode].pos - arrivedNode.pos)
                let align = currDir.x * nextDir.x + currDir.z * nextDir.z
                // Slight deterministic-ish variation for organic city branching while preferring straight flow
                let score = align + (nextRandFloat() - 0.5) * 0.35
                if score > bestScore {
                    bestScore = score
                    bestNextSeg = candIdx
                    bestForward = isFwd
                }
            }

            if bestNextSeg >= 0 {
                u.segIdx = bestNextSeg
                u.segForward = bestForward
                u.segT = bestForward ? 0.02 : 0.98
                let ns = roadGraph.segments[bestNextSeg]
                let safeHalfW = max(1.30, min(ns.halfWidthM - 1.35, ns.halfWidthM * 0.55))
                u.laneOffsetM = min(1.95, safeHalfW)
            } else {
                // Dead-end road: smooth U-turn back along segment
                u.segForward.toggle()
                u.segT = max(0.02, min(0.98, u.segT))
            }
        }

        let curSeg = roadGraph.segments[u.segIdx]
        let a = roadGraph.nodes[Int(curSeg.node0)].pos
        let b = roadGraph.nodes[Int(curSeg.node1)].pos
        let centerPos = a + (b - a) * max(0.0, min(1.0, u.segT))
        let travelVec = u.segForward ? (b - a) : (a - b)
        let targetYaw = atan2(travelVec.x, travelVec.z)

        // Smooth heading blend across junctions
        let yawDiff = normalizeAngle(targetYaw - u.yawRad)
        u.steerAngleRad = max(-0.45, min(0.45, yawDiff * 1.4))
        u.yawRad = normalizeAngle(u.yawRad + yawDiff * min(1.0, dt * 8.0))

        // Right-hand US lane offset clamped strictly inside legal asphalt half-width (zero sidewalk clipping!)
        let safeHalfW = max(1.30, min(curSeg.halfWidthM - 1.35, curSeg.halfWidthM * 0.62))
        let clampedLane = max(-safeHalfW, min(safeHalfW, u.laneOffsetM))
        let right = SIMD3<Float>(-cos(targetYaw), 0, sin(targetYaw))
        let targetPos = centerPos + right * clampedLane
        let oldPos = u.worldPosition
        u.worldPosition.x += (targetPos.x - u.worldPosition.x) * min(1.0, dt * 12.0)
        u.worldPosition.z += (targetPos.z - u.worldPosition.z) * min(1.0, dt * 12.0)
        // Baseline Y follows road node elevation only before GPU BVH aiRoadProbeKernel locks onto exact asphalt!
        if !u.hasRoadProbeY {
            u.worldPosition.y += (centerPos.y - u.worldPosition.y) * min(1.0, dt * 8.0)
        }
        clampToRoadCorridor(&u, dt: dt, alignYaw: true)
        u.velocityWorld = (u.worldPosition - oldPos) / max(1e-4, dt)
    }

    /// Enforces P1-2b legal asphalt corridor clamping + soft curb-repulsion yaw alignment so
    /// neither civilian traffic nor pursuing cops ever climb sidewalks or wedge into retaining walls.
    private func clampToRoadCorridor(_ u: inout RTAIInstanceState, dt: Float, alignYaw: Bool) {
        guard let near = roadGraph.nearestSegment(to: u.worldPosition, searchRadius: 48.0) else { return }
        let seg = roadGraph.segments[near.segIdx]
        let a = roadGraph.nodes[Int(seg.node0)].pos
        let b = roadGraph.nodes[Int(seg.node1)].pos
        let abXZ = SIMD2<Float>(b.x - a.x, b.z - a.z)
        let abLen = simd_length(abXZ)
        guard abLen > 0.25 else { return }
        var segDir = abXZ / abLen

        // Orient road tangent to match vehicle's travel hemisphere
        let carFwd = SIMD2<Float>(sin(u.yawRad), cos(u.yawRad))
        if simd_dot(carFwd, segDir) < 0.0 {
            segDir = -segDir
        }
        let segYaw = atan2(segDir.x, segDir.y)
        let segRight = SIMD2<Float>(-segDir.y, segDir.x) // screen right (-cosY, +sinY)

        let deltaXZ = SIMD2<Float>(u.worldPosition.x - near.point.x, u.worldPosition.z - near.point.z)
        let lat = simd_dot(deltaXZ, segRight)
        let safeHalfW = max(1.50, min(seg.halfWidthM - 1.25, seg.halfWidthM * 0.68))

        if abs(lat) > safeHalfW {
            let clampedLat = max(-safeHalfW, min(safeHalfW, lat))
            let corr = clampedLat - lat
            u.worldPosition.x += segRight.x * corr
            u.worldPosition.z += segRight.y * corr
            // Steer cleanly back parallel to road tangent when grazing curb
            let yawErr = normalizeAngle(segYaw - u.yawRad)
            u.yawRad = normalizeAngle(u.yawRad + yawErr * min(1.0, dt * 14.0))
        } else if alignYaw && abs(lat) > safeHalfW * 0.72 {
            // Soft curb-repulsion steering near road edge
            let edgeFrac = (abs(lat) - safeHalfW * 0.72) / max(0.25, safeHalfW * 0.28)
            let yawErr = normalizeAngle(segYaw - u.yawRad)
            u.yawRad = normalizeAngle(u.yawRad + yawErr * min(1.0, dt * (5.0 + 7.0 * edgeFrac)))
        }

        // Prevent sideways wall-wedging at pursuit speeds: keep heading within +-36 deg of road corridor tangent
        if alignYaw && u.speedMs > 10.0 {
            let errFromRoad = normalizeAngle(u.yawRad - segYaw)
            let maxDev: Float = 0.62 // ~35.5 deg
            if abs(errFromRoad) > maxDev {
                let clampedYaw = normalizeAngle(segYaw + max(-maxDev, min(maxDev, errFromRoad)))
                u.yawRad = normalizeAngle(u.yawRad + normalizeAngle(clampedYaw - u.yawRad) * min(1.0, dt * 16.0))
            }
        }
    }

    // MARK: - Heat 1-5 Police AI & Pursuit State Machine

    private func maintainPolicePopulation(dt: Float, playerPos: SIMD3<Float>, playerYaw: Float) {
        let cfg = heatConfigs[max(1, heatLevel)] ?? heatConfigs[1]!
        let desiredCops: Int
        switch pursuitPhase {
        case .pursuing, .sighted:
            desiredCops = cfg.maxActiveCops
        case .cooldown:
            desiredCops = max(1, cfg.maxActiveCops - 1)
        default:
            desiredCops = 1 // 1 free-roam patrol cruiser cruising nearby Rockport streets!
        }

        copSpawnTimer = max(0.0, copSpawnTimer - dt)
        let tactics: [RTCopTactic] = [.rearRam, .pitLeft, .pitRight, .boxInFront]

        for slot in 0..<Self.maxActiveCopsCap {
            if slot >= desiredCops {
                if aiSlots[slot].active {
                    let d = simd_distance(SIMD2<Float>(aiSlots[slot].worldPosition.x, aiSlots[slot].worldPosition.z),
                                          SIMD2<Float>(playerPos.x, playerPos.z))
                    if d > 240.0 || pursuitPhase == .freeRoam {
                        aiSlots[slot].active = false
                    }
                }
                continue
            }

            if aiSlots[slot].active {
                let d = simd_distance(SIMD2<Float>(aiSlots[slot].worldPosition.x, aiSlots[slot].worldPosition.z),
                                      SIMD2<Float>(playerPos.x, playerPos.z))
                if d > 310.0 {
                    aiSlots[slot].active = false
                }
            }

            if !aiSlots[slot].active && copSpawnTimer <= 0.0 {
                // Pause fresh pursuing reinforcements when player is actively breaking LOS so Cooldown is genuinely achievable!
                if pursuitPhase == .pursuing && losLostTimer > 0.35 { continue }
                let preferBehind = (pursuitPhase == .pursuing)
                let spawnMin: Float = (pursuitPhase == .cooldown) ? 145.0 : ((pursuitPhase == .pursuing) ? 130.0 : 85.0)
                let spawnMax: Float = (pursuitPhase == .cooldown) ? 250.0 : 230.0
                if let placed = spawnUnitOnRoad(near: playerPos, playerYaw: playerYaw, minDist: spawnMin, maxDist: spawnMax, preferBehind: preferBehind) {
                    var u = aiSlots[slot]
                    u.active = true
                    u.isCop = true
                    u.archetype = (pursuitPhase == .freeRoam || pursuitPhase == .patrol) ? .copMidsize : cfg.copArchetype
                    u.worldPosition = placed.pos
                    u.prevWorldPosition = placed.pos
                    u.yawRad = placed.yaw
                    u.prevYawRad = placed.yaw
                    u.segIdx = placed.segIdx
                    u.segT = placed.t
                    u.segForward = placed.forward
                    u.cruiseSpeedMs = 20.0
                    u.speedMs = (pursuitPhase == .pursuing) ? 28.0 : 18.0
                    u.sirenOn = (pursuitPhase == .pursuing || pursuitPhase == .sighted || pursuitPhase == .cooldown)
                    u.lightbarPhase = Float(slot) * 1.57
                    u.tactic = u.sirenOn ? tactics[slot % tactics.count] : .patrol
                    aiSlots[slot] = u
                    copSpawnTimer = (pursuitPhase == .pursuing) ? max(1.2, cfg.spawnIntervalSec * 0.35) : 4.0
                }
            }
        }
    }

    private func stepPoliceUnit(index: Int, dt: Float, playerPos: SIMD3<Float>, playerVel: SIMD3<Float>, playerSpeedMs: Float) {
        var u = aiSlots[index]
        u.lightbarPhase += dt * 16.0

        let isActivelyChasing = (pursuitPhase == .pursuing || pursuitPhase == .sighted) && u.sirenOn
        if !isActivelyChasing {
            // Patrol / Cooldown search: cruise road graph at brisk patrol speed
            let cfg = heatConfigs[max(1, heatLevel)] ?? heatConfigs[1]!
            u.cruiseSpeedMs = (pursuitPhase == .cooldown) ? min(36.0, cfg.maxCopSpeedMs * 0.55) : 19.0
            u.speedMs += (u.cruiseSpeedMs - u.speedMs) * min(1.0, dt * 4.0)
            if u.segIdx >= 0 && u.segIdx < roadGraph.segments.count {
                advanceAlongRoadGraph(&u, dt: dt)
            }
            u.wheelSpinRad += (u.speedMs / 0.34) * dt
            aiSlots[index] = u
            return
        }

        // Active Pursuit Intercept / Rolling Box-In / PIT Steering (strictly inside legal asphalt corridor!)
        let cfg = heatConfigs[max(1, heatLevel)] ?? heatConfigs[1]!
        let toPlayerXZ = SIMD2<Float>(playerPos.x - u.worldPosition.x, playerPos.z - u.worldPosition.z)
        let dist = max(0.1, simd_length(toPlayerXZ))

        let pFwd = (playerSpeedMs > 2.0)
            ? simd_normalize(SIMD3<Float>(playerVel.x, 0, playerVel.z))
            : SIMD3<Float>(sin(u.yawRad), 0, cos(u.yawRad))
        let pRight = SIMD3<Float>(-pFwd.z, 0, pFwd.x)
        let copFwd = SIMD3<Float>(sin(u.yawRad), 0, cos(u.yawRad))

        // Check whether cop is ahead of the player (rolling roadblock / box-in formation) vs chasing from behind
        let aheadOfPlayer = (u.worldPosition.x - playerPos.x) * pFwd.x + (u.worldPosition.z - playerPos.z) * pFwd.z
        let sameHeading = (copFwd.x * pFwd.x + copFwd.z * pFwd.z) > 0.25
        let isRollingLead = (aheadOfPlayer > -2.0 && dist < 85.0 && sameHeading)

        var targetWorld: SIMD3<Float>
        var desiredSpeed: Float

        if isRollingLead {
            // Rolling Box-In / Lead Pace Interceptor: race down the road corridor ahead of player in crisp formation
            // (never perform suicidal 180-deg U-turns into sidewalks or retaining walls!)
            let lookAheadPos = u.worldPosition + copFwd * 22.0
            let laneSign: Float = (u.tactic == .pitLeft) ? -1.0 : ((u.tactic == .pitRight) ? 1.0 : ((index % 2 == 0) ? 0.65 : -0.65))
            if let nearRoad = roadGraph.nearestSegment(to: lookAheadPos, searchRadius: 60.0) {
                let seg = roadGraph.segments[nearRoad.segIdx]
                let a = roadGraph.nodes[Int(seg.node0)].pos
                let b = roadGraph.nodes[Int(seg.node1)].pos
                var sDir = simd_normalize(SIMD3<Float>(b.x - a.x, 0, b.z - a.z))
                if (sDir.x * pFwd.x + sDir.z * pFwd.z) < 0.0 { sDir = -sDir }
                let sRight = SIMD3<Float>(-sDir.z, 0, sDir.x)
                let safeHalfW = max(1.45, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.62))
                let targetLat = max(-safeHalfW * 0.80, min(safeHalfW * 0.80, laneSign * min(2.0, safeHalfW * 0.78)))
                targetWorld = nearRoad.point + sDir * 18.0 + sRight * targetLat
                if !u.hasRoadProbeY {
                    u.worldPosition.y += (nearRoad.point.y - u.worldPosition.y) * min(1.0, dt * 6.0)
                }
            } else {
                targetWorld = u.worldPosition + pFwd * 24.0 + pRight * (laneSign * 1.6)
            }
            // Pace slightly slower when far ahead so player closes into a dramatic rolling box-in!
            let paceDelta: Float = (aheadOfPlayer > 26.0) ? -3.5 : ((aheadOfPlayer < 12.0) ? 2.5 : -0.8)
            desiredSpeed = min(cfg.maxCopSpeedMs, max(20.0, playerSpeedMs + paceDelta))
        } else {
            // Trailing Pursuit / PIT Maneuver: chase player while clamping lateral flank offset inside legal asphalt
            let leadSec = min(1.2, dist / max(20.0, u.speedMs))
            targetWorld = playerPos + playerVel * (leadSec * 0.50)
            switch u.tactic {
            case .pitLeft:
                targetWorld += -pRight * 2.1 - pFwd * 1.2
            case .pitRight:
                targetWorld += pRight * 2.1 - pFwd * 1.2
            case .boxInFront:
                targetWorld += pFwd * 10.0
            case .rearRam, .patrol:
                targetWorld += -pFwd * 1.0
            }

            // Road corridor attraction so trailing cops hug Rockport avenues rather than cutting corners across curbs
            if let nearRoad = roadGraph.nearestSegment(to: targetWorld, searchRadius: 64.0) {
                let seg = roadGraph.segments[nearRoad.segIdx]
                let a = roadGraph.nodes[Int(seg.node0)].pos
                let b = roadGraph.nodes[Int(seg.node1)].pos
                let abXZ = SIMD2<Float>(b.x - a.x, b.z - a.z)
                let abLen = max(0.25, simd_length(abXZ))
                let sDir = abXZ / abLen
                let sRight = SIMD2<Float>(-sDir.y, sDir.x)
                let tDelta = SIMD2<Float>(targetWorld.x - nearRoad.point.x, targetWorld.z - nearRoad.point.z)
                let tLat = simd_dot(tDelta, sRight)
                let safeHalfW = max(1.45, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.64))
                if abs(tLat) > safeHalfW {
                    let cLat = max(-safeHalfW, min(safeHalfW, tLat))
                    targetWorld.x = nearRoad.point.x + sRight.x * cLat
                    targetWorld.z = nearRoad.point.z + sRight.y * cLat
                }
                if dist > 40.0 {
                    let roadBlend: Float = min(0.55, (dist - 40.0) / 120.0)
                    targetWorld.x = targetWorld.x * (1.0 - roadBlend) + nearRoad.point.x * roadBlend
                    targetWorld.z = targetWorld.z * (1.0 - roadBlend) + nearRoad.point.z * roadBlend
                }
                if !u.hasRoadProbeY {
                    u.worldPosition.y += (nearRoad.point.y - u.worldPosition.y) * min(1.0, dt * 6.0)
                }
            } else if !u.hasRoadProbeY {
                u.worldPosition.y += (playerPos.y - u.worldPosition.y) * min(1.0, dt * 6.0)
            }

            let rubberBandBonus: Float = (dist > 45.0) ? min(18.0, (dist - 45.0) * 0.18) : 0.0
            desiredSpeed = min(cfg.maxCopSpeedMs + rubberBandBonus, max(18.0, playerSpeedMs + 8.0 + rubberBandBonus))
        }

        let desiredYaw = atan2(targetWorld.x - u.worldPosition.x, targetWorld.z - u.worldPosition.z)
        let yawErr = normalizeAngle(desiredYaw - u.yawRad)
        u.steerAngleRad = max(-0.48, min(0.48, yawErr * 1.4))
        let maxTurnRate: Float = isRollingLead ? 1.85 : 2.35
        u.yawRad = normalizeAngle(u.yawRad + max(-maxTurnRate * dt, min(maxTurnRate * dt, yawErr * 4.5 * dt)))

        let cornerSlowdown: Float = 1.0 - min(0.42, abs(yawErr) * 0.35)
        let targetSpeed = max(16.0, desiredSpeed * cornerSlowdown)
        let accelRate: Float = (targetSpeed > u.speedMs) ? 14.0 : 18.0
        u.speedMs = max(0.0, u.speedMs + max(-accelRate * dt, min(accelRate * dt, targetSpeed - u.speedMs)))

        let fwd = SIMD3<Float>(sin(u.yawRad), 0, cos(u.yawRad))
        let oldPos = u.worldPosition
        u.worldPosition += fwd * (u.speedMs * dt)
        clampToRoadCorridor(&u, dt: dt, alignYaw: true)
        u.velocityWorld = (u.worldPosition - oldPos) / max(1e-4, dt)
        u.wheelSpinRad += (u.speedMs / 0.34) * dt
        aiSlots[index] = u
    }

    private func stepPursuitFSM(
        dt: Float,
        playerPos: SIMD3<Float>,
        playerYaw: Float,
        playerSpeedKmh: Float,
        nearestCopDist: Float,
        anyCopHasLOS: Bool
    ) {
        switch pursuitPhase {
        case .freeRoam, .patrol:
            // Auto-trigger Heat 1 pursuit if player blasts past a patrol cruiser > 135 km/h
            if nearestCopDist < 48.0 && playerSpeedKmh > 135.0 {
                pursuitPhase = .sighted
                heatLevel = max(1, heatLevel)
                bounty += 1000.0
                infractionsCount += 1
                sirenAudio.radarPing = true
                for i in 0..<Self.maxActiveCopsCap where aiSlots[i].active {
                    aiSlots[i].sirenOn = true
                }
            }

        case .sighted:
            pursuitPhase = .pursuing
            losLostTimer = 0.0
            triggerInstantPursuit(near: playerPos, playerYaw: playerYaw, setHeat: max(1, heatLevel))

        case .pursuing:
            pursuitDurationSec += dt
            let cfg = heatConfigs[max(1, heatLevel)] ?? heatConfigs[1]!
            bounty += cfg.bountyPerSec * dt

            // Automatic Heat Escalation (Heat 1->5 based on sustained pursuit duration & bounty)
            let autoHeat = min(5, 1 + Int(pursuitDurationSec / 45.0))
            if autoHeat > heatLevel {
                heatLevel = autoHeat
                sirenAudio.radarPing = true
            }

            // Busted check: pinned slow (< 14 km/h) with cop within 14m
            if playerSpeedKmh < 14.0 && nearestCopDist < 14.0 {
                bustedProgress = min(1.0, bustedProgress + dt * 0.25) // 4.0s to bust
                if bustedProgress >= 1.0 {
                    pursuitPhase = .busted
                    outcomeBannerTimer = 4.0
                }
            } else {
                bustedProgress = max(0.0, bustedProgress - dt * 0.40)
            }

            // Line-of-sight check for Cooldown transition
            if !anyCopHasLOS {
                losLostTimer += dt
                if losLostTimer >= 3.0 {
                    pursuitPhase = .cooldown
                    evadeProgress = 0.05
                    sirenAudio.radarPing = true
                }
            } else {
                losLostTimer = 0.0
                evadeProgress = max(0.0, evadeProgress - dt * 0.35)
            }

        case .cooldown:
            if anyCopHasLOS && nearestCopDist < 95.0 {
                // Re-spotted! Back into active pursuit
                pursuitPhase = .pursuing
                losLostTimer = 0.0
                sirenAudio.radarPing = true
            } else {
                bustedProgress = max(0.0, bustedProgress - dt * 0.5)
                evadeProgress = min(1.0, evadeProgress + dt / 12.0) // 12s clean cooldown to evade
                if evadeProgress >= 1.0 {
                    pursuitPhase = .evaded
                    bounty += Float(heatLevel) * 5000.0
                    outcomeBannerTimer = 4.0
                    for i in 0..<Self.maxActiveCopsCap {
                        aiSlots[i].sirenOn = false
                        aiSlots[i].tactic = .patrol
                    }
                }
            }

        case .evaded, .busted:
            outcomeBannerTimer -= dt
            if outcomeBannerTimer <= 0.0 {
                clearPursuit()
            }
        }
    }

    private func resolvePlayerCollision(slotIdx: Int, playerPos: SIMD3<Float>, playerVel: SIMD3<Float>) {
        var u = aiSlots[slotIdx]
        let delta = SIMD2<Float>(playerPos.x - u.worldPosition.x, playerPos.z - u.worldPosition.z)
        let dist = simd_length(delta)
        let minSep: Float = u.archetype.halfWidth + 1.05
        guard dist < minSep && dist > 0.01 else { return }

        let normalXZ = delta / dist
        let overlap = minSep - dist
        let relVelXZ = SIMD2<Float>(playerVel.x - u.velocityWorld.x, playerVel.z - u.velocityWorld.z)
        let closing = -(relVelXZ.x * normalXZ.x + relVelXZ.y * normalXZ.y)

        // Separate AI unit slightly (clamped inside legal asphalt corridor!) and accumulate player bounce impulse
        u.worldPosition.x -= normalXZ.x * overlap * 0.65
        u.worldPosition.z -= normalXZ.y * overlap * 0.65
        u.speedMs *= 0.72
        clampToRoadCorridor(&u, dt: 0.016, alignYaw: true)
        aiSlots[slotIdx] = u

        let push = max(overlap * 4.0, max(0.0, closing) * 0.55)
        playerBumpImpulse += SIMD3<Float>(normalXZ.x * push, 0, normalXZ.y * push)
        let impact = max(0.15, min(1.0, abs(closing) / 28.0))
        playerCollisionIntensity = max(playerCollisionIntensity, impact)

        // Ramming a cop car escalates bounty & triggers pursuit!
        if u.isCop && impact > 0.25 {
            bounty += 2500.0
            infractionsCount += 1
            if pursuitPhase == .freeRoam || pursuitPhase == .patrol {
                pursuitPhase = .sighted
                heatLevel = max(1, heatLevel)
            }
        }
    }

    // MARK: - Cop RT Lightbars (RTDynamicFX[13]) & 3D Doppler Siren Audio

    private func updateCopLightbarsAndSiren(playerPos: SIMD3<Float>, playerVel: SIMD3<Float>, playerYaw: Float) {
        var activeCops: [(idx: Int, dist: Float)] = []
        for i in 0..<Self.maxActiveCopsCap where aiSlots[i].active && aiSlots[i].sirenOn {
            let d = simd_distance(aiSlots[i].worldPosition, playerPos)
            activeCops.append((i, d))
        }
        activeCops.sort { $0.dist < $1.dist }

        // Fill up to 4 nearest cop RT lightbars (alternating vivid crimson/cobalt strobes calibrated for RayTracing.metal *22.0 HDR scale!)
        for k in 0..<4 {
            if k < activeCops.count {
                let u = aiSlots[activeCops[k].idx]
                let cy = cos(u.yawRad), sy = sin(u.yawRad)
                let loc = u.archetype.localLightbarOffset
                let worldBar = u.worldPosition + SIMD3<Float>(loc.x * cy + loc.z * sy, loc.y + 0.12, -loc.x * sy + loc.z * cy)
                let strobe = sin(u.lightbarPhase + Float(k) * 1.57)
                let isRed = strobe >= 0.0
                // Normalized 0..1 strobe duty & unit RGB color (RayTracing.metal applies *22.0 radiance & volumetric halo!)
                let strobeDuty = Float(0.42 + 0.58 * abs(strobe))
                copLights[k] = RTCopLightbarLight(
                    worldPos: worldBar,
                    strobePhase: strobeDuty,
                    colorHDR: isRed ? SIMD3<Float>(1.00, 0.07, 0.04) : SIMD3<Float>(0.06, 0.28, 1.00),
                    intensity: 0.78
                )
            } else {
                copLights[k] = RTCopLightbarLight()
            }
        }

        // Nearest pursuing cop -> 3D stereo-panned Doppler siren for RTAudioEngine
        if let nearest = activeCops.first, nearest.dist < 190.0 {
            let u = aiSlots[nearest.idx]
            let dist = max(1.0, nearest.dist)
            let gain = max(0.0, min(1.0, 1.0 - (dist - 8.0) / 165.0))
            let toCop = (u.worldPosition - playerPos) / dist
            let playerRight = SIMD3<Float>(-cos(playerYaw), 0, sin(playerYaw))
            let pan = max(-1.0, min(1.0, simd_dot(toCop, playerRight)))

            // Radial relative velocity -> acoustic Doppler shift (speed of sound = 343 m/s)
            let relVel = u.velocityWorld - playerVel
            let closingSpeed = -simd_dot(relVel, toCop)
            let doppler = max(0.78, min(1.28, 343.0 / max(220.0, 343.0 - closingSpeed)))
            let sirenMode: Int32 = (dist < 28.0) ? 2 : ((dist < 70.0) ? 1 : 0)

            sirenAudio.gain = gain * gain
            sirenAudio.pan = pan
            sirenAudio.doppler = doppler
            sirenAudio.mode = sirenMode
        } else {
            sirenAudio.gain = 0.0
            sirenAudio.pan = 0.0
            sirenAudio.doppler = 1.0
            sirenAudio.mode = 0
        }
    }

    // MARK: - Fast Heading-Up GPU Minimap Packer (<= 2 KB, < 0.1 ms CPU)

    func packMinimap(playerPos: SIMD3<Float>, playerYaw: Float, mapRangeM: Float = 175.0) -> RTMinimapBuffer {
        var buf = RTMinimapBuffer()
        buf.mapRangeM = max(60.0, mapRangeM)
        buf.playerYawRad = playerYaw

        let invR: Float = 1.0 / buf.mapRangeM
        let cy = cos(playerYaw)
        let sy = sin(playerYaw)

        // Transform world XZ delta -> heading-up normalized [-1..+1] (matching RTRenderer screen-right basis):
        //   +x = screen right = -dx * cos(yaw) + dz * sin(yaw)
        //   +y = car forward  =  dx * sin(yaw) + dz * cos(yaw)
        @inline(__always)
        func toMinimap(_ worldPos: SIMD3<Float>) -> SIMD2<Float> {
            let dx = worldPos.x - playerPos.x
            let dz = worldPos.z - playerPos.z
            let rx = (-dx * cy + dz * sy) * invR
            let ry = ( dx * sy + dz * cy) * invR
            return SIMD2<Float>(rx, ry)
        }

        // 1. Pack up to 96 closest road segments within mapRangeM * 1.15
        let cands = roadGraph.querySegments(near: SIMD2<Float>(playerPos.x, playerPos.z), radius: buf.mapRangeM * 1.15)
        if !cands.isEmpty {
            var ranked: [(segIdx: Int, dSq: Float)] = []
            ranked.reserveCapacity(cands.count)
            let maxRSq = (buf.mapRangeM * 1.25) * (buf.mapRangeM * 1.25)
            for sIdx in cands {
                let seg = roadGraph.segments[sIdx]
                let p0 = roadGraph.nodes[Int(seg.node0)].pos
                let p1 = roadGraph.nodes[Int(seg.node1)].pos
                let mx = 0.5 * (p0.x + p1.x) - playerPos.x
                let mz = 0.5 * (p0.z + p1.z) - playerPos.z
                let dSq = mx * mx + mz * mz
                if dSq <= maxRSq {
                    ranked.append((sIdx, dSq))
                }
            }
            if ranked.count > RTMinimapBuffer.maxSegments {
                ranked.sort { $0.dSq < $1.dSq }
            }
            let nSeg = min(ranked.count, RTMinimapBuffer.maxSegments)
            for i in 0..<nSeg {
                let seg = roadGraph.segments[ranked[i].segIdx]
                let m0 = toMinimap(roadGraph.nodes[Int(seg.node0)].pos)
                let m1 = toMinimap(roadGraph.nodes[Int(seg.node1)].pos)
                buf.setSegment(i, SIMD4<Float>(m0.x, m0.y, m1.x, m1.y))
            }
            buf.segCount = UInt32(nSeg)
        }

        // 2. Pack up to 24 AI blips (cops prioritized first so pursuing units NEVER drop off radar!)
        var blipIdx = 0
        for pass in 0..<2 {
            let wantCops = (pass == 0)
            for i in 0..<Self.maxAISlots where aiSlots[i].active && (aiSlots[i].isCop == wantCops) {
                guard blipIdx < RTMinimapBuffer.maxBlips else { break }
                let u = aiSlots[i]
                var mp = toMinimap(u.worldPosition)
                let len = simd_length(mp)
                if len > 1.05 {
                    if !wantCops { continue }
                    // Pin distant pursuing cops cleanly onto radar perimeter edge!
                    mp = (mp / len) * 0.94
                }
                let bType: Float = u.isCop ? (u.sirenOn ? 2.0 : 1.0) : 0.0
                buf.setBlip(blipIdx, SIMD4<Float>(mp.x, mp.y, bType, u.lightbarPhase))
                blipIdx += 1
            }
        }
        buf.blipCount = UInt32(blipIdx)
        return buf
    }

    /// Populates pursuit HUD fields directly into RTHudExtUniforms.
    func fillHudExt(_ ext: inout RTHudExtUniforms) {
        ext.heatLevel = Float(heatLevel)
        ext.bounty = bounty
        ext.evadeProgress = evadeProgress
        ext.bustedProgress = bustedProgress
        ext.copCount = UInt32(max(0, activeCopCount))
        ext.simTimeSec = simClock

        if pursuitPhase == .pursuing || pursuitPhase == .sighted || pursuitPhase == .cooldown {
            ext.hudFlags |= RTHudFlagBits.pursuitActive
        } else {
            ext.hudFlags &= ~RTHudFlagBits.pursuitActive
        }
        if pursuitPhase == .busted { ext.hudFlags |= RTHudFlagBits.busted }
        if pursuitPhase == .evaded { ext.hudFlags |= RTHudFlagBits.evaded }
    }

    // MARK: - Spawning & Math Utilities

    private func spawnUnitOnRoad(
        near playerPos: SIMD3<Float>,
        playerYaw: Float,
        minDist: Float,
        maxDist: Float,
        preferBehind: Bool,
        preferAhead: Bool = false
    ) -> (pos: SIMD3<Float>, yaw: Float, segIdx: Int, t: Float, forward: Bool)? {
        let cands = roadGraph.querySegments(near: SIMD2<Float>(playerPos.x, playerPos.z), radius: maxDist)
        guard !cands.isEmpty else { return nil }

        let pFwd = SIMD2<Float>(sin(playerYaw), cos(playerYaw))
        var bestFallback: (pos: SIMD3<Float>, yaw: Float, segIdx: Int, t: Float, forward: Bool)? = nil

        for _ in 0..<18 {
            let sIdx = cands[Int(nextRand() % UInt64(cands.count))]
            let seg = roadGraph.segments[sIdx]
            let p0 = roadGraph.nodes[Int(seg.node0)].pos
            let p1 = roadGraph.nodes[Int(seg.node1)].pos
            let t = 0.2 + nextRandFloat() * 0.6
            let center = p0 + (p1 - p0) * t
            let delta = SIMD2<Float>(center.x - playerPos.x, center.z - playerPos.z)
            let d = simd_length(delta)
            if d < minDist || d > maxDist { continue }

            let fwdFlag = (nextRand() & 1) == 0
            let tv = fwdFlag ? (p1 - p0) : (p0 - p1)
            let yaw = atan2(tv.x, tv.z)
            let right = SIMD3<Float>(-cos(yaw), 0, sin(yaw))
            let safeHalfW = max(1.30, min(seg.halfWidthM - 1.35, seg.halfWidthM * 0.55))
            let laneOff = min(1.95, safeHalfW)
            let pos = center + right * laneOff
            let rec = (pos, yaw, sIdx, t, fwdFlag)

            if preferBehind {
                let dotFwd = (delta.x * pFwd.x + delta.y * pFwd.y) / max(1.0, d)
                if dotFwd < 0.25 {
                    return rec
                }
                bestFallback = rec
            } else if preferAhead {
                let dotFwd = (delta.x * pFwd.x + delta.y * pFwd.y) / max(1.0, d)
                if dotFwd > 0.35 {
                    return rec
                }
                bestFallback = rec
            } else {
                return rec
            }
        }
        return bestFallback
    }

    @inline(__always)
    private func normalizeAngle(_ a: Float) -> Float {
        var r = a.truncatingRemainder(dividingBy: 2.0 * .pi)
        if r > .pi { r -= 2.0 * .pi }
        if r < -.pi { r += 2.0 * .pi }
        return r
    }

    @inline(__always)
    private func nextRand() -> UInt64 {
        // Fast deterministic SplitMix64 / Xorshift64*
        rngState ^= rngState >> 12
        rngState ^= rngState << 25
        rngState ^= rngState >> 27
        return rngState &* 2685821657736338717
    }

    @inline(__always)
    private func nextRandFloat() -> Float {
        return Float(nextRand() & 0x00FF_FFFF) / Float(0x0100_0000)
    }
}
