import Foundation
import Metal
import simd

// MARK: - Locked v1.2.0 Metal ABI Structs (Shared w/ RayTracing.metal & RTHudTypes.swift)

/// 128-byte 16B-aligned GPU uniform block bound at `raytraceKernel [[buffer(16)]]`.
/// Matches `RayTracing.metal` `struct RTEventWorldFX` byte-for-byte (per #96 ABI lock):
///   - `gatePosRadius`: `(x, y, z, radiusMeters)` active checkpoint/speedtrap gate
///   - `gateDirKind`:   `(fwdX, fwdY, fwdZ, kindFloat)` where `0=sprint, 1=speedtrap, 2=boss`
///   - `nextPosRadius`: `(nx, ny, nz, nextRadiusMeters)` upcoming gate (`w == 0` if none/finish)
///   - `fxParams`:      `(gateActive [0/1], isSpeedtrap [0/1], radarFlash [0..1], simTimeSec)`
///   - `chevron0..3`:   up to 4 floating directional road chevrons `(x, y, z, yawRad)` (`w <= -100` = inactive; shader lifts +1.65m)
struct RTEventWorldFX {
    var gatePosRadius: SIMD4<Float> = .zero
    var gateDirKind: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 1.0, 0.0)
    var nextPosRadius: SIMD4<Float> = .zero
    var fxParams: SIMD4<Float> = .zero
    var chevron0: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 0.0, -100.0)
    var chevron1: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 0.0, -100.0)
    var chevron2: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 0.0, -100.0)
    var chevron3: SIMD4<Float> = SIMD4<Float>(0.0, 0.0, 0.0, -100.0)

    static let byteSize = 128

    static func assertLayout() {
        precondition(
            MemoryLayout<RTEventWorldFX>.stride == byteSize,
            "RTEventWorldFX stride (\(MemoryLayout<RTEventWorldFX>.stride)) must be \(byteSize) bytes"
        )
        precondition(
            MemoryLayout<RTEventWorldFX>.alignment == 16,
            "RTEventWorldFX alignment (\(MemoryLayout<RTEventWorldFX>.alignment)) must be 16 bytes"
        )
    }

    static func inactive(simTime: Float = 0.0, radarFlash: Float = 0.0) -> RTEventWorldFX {
        var fx = RTEventWorldFX()
        fx.fxParams = SIMD4<Float>(0.0, 0.0, radarFlash, simTime)
        return fx
    }

    mutating func setChevron(_ index: Int, _ value: SIMD4<Float>) {
        switch index {
        case 0: chevron0 = value
        case 1: chevron1 = value
        case 2: chevron2 = value
        case 3: chevron3 = value
        default: break
        }
    }
}

/// Interactive marker displayed on the O(1) Full-Island World Map overlay (up to 24 markers).
struct RTWorldMapMarker {
    /// 0 = landmark, 1 = sprint, 2 = speedtrap, 3 = boss
    var kind: UInt32
    var rank: UInt32
    var worldPos: SIMD3<Float>
    var yawRad: Float
    var normUV: SIMD2<Float>
    var title: String
    var subtitle: String
    var eventId: String?
    var landmarkIndex: Int?
}

/// Clean Swift HUD snapshot consumed by `RTRenderer.swift` to pack `RTEventHudUniforms` at `buffer(5)`.
struct RTEventHudState {
    var mode: UInt32 = 0             // 0=none, 1=sprint, 2=speedtrap, 3=boss
    var state: UInt32 = 0            // 0=idle, 1=countdown, 2=active, 3=win, 4=lose
    var countdownSec: Float = 0.0
    var cpIndex: UInt32 = 0          // checkpoints/traps cleared so far (0..cpTotal)
    var cpTotal: UInt32 = 0
    var elapsedSec: Float = 0.0
    var nextGateDistM: Float = 0.0
    var targetScore: Float = 0.0     // cumulative km/h for speedtrap, par sec for sprint/boss
    var currentScore: Float = 0.0    // cumulative km/h for speedtrap, elapsed sec for sprint/boss
    var lastTrapSpeedKmh: Float = 0.0
    var radarFlash: Float = 0.0      // 0..1 white radar flash intensity
    var rank: UInt32 = 1             // 1 or 2
    var bossGapM: Float = 0.0        // positive = player ahead, negative = boss ahead
    var bossRank: UInt32 = 0         // 1..15 for Blacklist boss, 0 otherwise
    var eventId: String = ""
    var eventTitle: String = ""
    var bossName: String = ""
    var bossCarName: String = ""
    var bossColorRGB: SIMD3<Float> = SIMD3<Float>(1.0, 0.72, 0.14)

    // Interactive Full-Island World Map state
    var worldMapOpen: Bool = false
    var mapCenterNorm: SIMD2<Float> = SIMD2<Float>(0.5, 0.5)
    var mapCenterXZ: SIMD2<Float> = .zero
    var mapZoom: Float = 1.25
    /// Square padded (minX, minZ, maxX, maxZ) matching baked 1024x1024 world map UV [0..1]^2.
    var worldBoundsMinMax: SIMD4<Float> = SIMD4<Float>(-2500.0, -2500.0, 2500.0, 2500.0)
    var selectedMarkerIndex: Int = 0
    var selectedMarkerTitle: String = ""
    var selectedMarkerSubtitle: String = ""
    var selectedMarkerKind: UInt32 = 0
    var mapMarkers: [RTWorldMapMarker] = []     // up to 24
    var gpsRoutePointsNorm: [SIMD2<Float>] = [] // up to 16 normalized [0..1]^2 UVs
    var gpsRoutePointsXZ: [SIMD2<Float>] = []   // up to 16 world XZ meters
}

// MARK: - Event Catalog PODs & World Map Actions

enum RTEventKind: String, CaseIterable {
    case sprint = "sprint"
    case speedtrap = "speedtrap"
    case boss = "boss"

    var hudMode: UInt32 {
        switch self {
        case .sprint:    return 1
        case .speedtrap: return 2
        case .boss:      return 3
        }
    }

    var fxKindFloat: Float {
        switch self {
        case .sprint:    return 0.0
        case .speedtrap: return 1.0
        case .boss:      return 2.0
        }
    }

    var markerKind: UInt32 {
        switch self {
        case .sprint:    return 1
        case .speedtrap: return 2
        case .boss:      return 3
        }
    }
}

enum RTEventFSMState: UInt32 {
    case idle      = 0
    case countdown = 1
    case active    = 2
    case win       = 3
    case lose      = 4
}

enum RTWorldMapAction: Equatable {
    case none
    case teleport(pos: SIMD3<Float>, yaw: Float, label: String)
    case startEvent(id: String)
}

struct RTEventCheckpoint {
    var pos: SIMD3<Float>
    var yawRad: Float
    var radius: Float
    var roadNodeIdx: Int
    var isSpeedtrap: Bool
}

struct RTEventWaypoint {
    var pos: SIMD3<Float>
    var yawRad: Float
    var cumDistM: Float
}

struct RTEventDefinition {
    var id: String
    var kind: RTEventKind
    var title: String
    var trackName: String
    var district: String
    var districtIndex: Int
    var sourceEventId: String
    var bossRank: Int
    var bossName: String
    var bossFullName: String
    var bossCarKey: String
    var bossCarName: String
    var bossUpgradeMultiplier: Float
    var bossPaintRGB: SIMD3<Float>
    var bountyReward: Int
    var targetScore: Float
    var parTimeSec: Float
    var startPos: SIMD3<Float>
    var startYawRad: Float
    var routeLengthM: Float
    var checkpoints: [RTEventCheckpoint]
    var routeWaypoints: [RTEventWaypoint]
}

struct RTEventStartResult {
    let event: RTEventDefinition
    let startPos: SIMD3<Float>
    let startYawRad: Float
}

// MARK: - Private JSON Decodable Mirrors (Graceful Fallback)

private struct RawEventsCatalogJSON: Decodable {
    let schemaVersion: Int?
    let world: RawWorldMetaJSON?
    let events: [RawEventJSON]?
}

private struct RawWorldMetaJSON: Decodable {
    let roadBoundsMinXZ: [Float]?
    let roadBoundsMaxXZ: [Float]?
    let landmarks: [RawLandmarkJSON]?
}

private struct RawLandmarkJSON: Decodable {
    let index: Int?
    let name: String
    let spawn_pos: [Float]
    let spawn_yaw_rad: Float?
    let region_id: Int?
}

private struct RawCheckpointJSON: Decodable {
    let pos: [Float]
    let yawRad: Float?
    let radius: Float?
    let roadNodeIdx: Int?
    let isSpeedtrap: Bool?
}

private struct RawEventJSON: Decodable {
    let id: String
    let kind: String
    let title: String
    let trackName: String?
    let district: String?
    let districtIndex: Int?
    let sourceEventId: String?
    let bossRank: Int?
    let bossName: String?
    let bossFullName: String?
    let bossCarKey: String?
    let bossCarName: String?
    let bossUpgradeMultiplier: Float?
    let bossPaintRGB: [Float]?
    let bountyReward: Int?
    let targetScore: Float?
    let parTimeSec: Float?
    let startPos: [Float]
    let startYawRad: Float?
    let routeLengthM: Float?
    let checkpoints: [RawCheckpointJSON]
    let routeWaypoints: [[Float]]?
}

// MARK: - NFSMW Event Manager + O(1) Full-Island World Map Baker

final class RTEventManager {
    static let maxMapMarkers = 24
    static let maxGPSRoutePoints = 16
    static let worldMapTextureSize = 1024
    static let radarFlashDurationSec: Float = 0.45
    static let defaultCountdownSec: Float = 2.5

    // Loaded catalog
    private(set) var events: [RTEventDefinition] = []
    private(set) var eventsById: [String: RTEventDefinition] = [:]
    private(set) var landmarks: [RkptLandmark] = []
    private(set) var isCatalogLoaded: Bool = false

    // Active Event FSM state
    private(set) var activeEvent: RTEventDefinition? = nil
    private(set) var eventState: RTEventFSMState = .idle
    private(set) var countdownSec: Float = 0.0
    private(set) var currentCheckpointIndex: Int = 0
    private(set) var elapsedSec: Float = 0.0
    private(set) var currentScore: Float = 0.0
    private(set) var lastTrapSpeedKmh: Float = 0.0
    private(set) var radarFlash: Float = 0.0
    private(set) var nextGateDistM: Float = 0.0
    private(set) var playerRank: UInt32 = 1
    private(set) var bossGapMeters: Float = 0.0
    private(set) var trapSpeedsKmh: [Float] = []

    // Dedicated Blacklist Boss AI opponent (designed to claim AI TLAS slot 0 during Boss Duels)
    private(set) var bossInstanceState: RTAIInstanceState? = nil
    private var bossDistAlongRouteM: Float = 0.0
    private var bossWaypointCursor: Int = 0
    private var playerDistAlongRouteM: Float = 0.0
    private var simClockSec: Float = 0.0

    // Pre-baked O(1) 1024x1024 Full-Island World Map texture & bounds
    private(set) var worldMapTexture: MTLTexture? = nil
    private(set) var worldBoundsMinMax: SIMD4<Float> = SIMD4<Float>(-2500.0, -2500.0, 2500.0, 2500.0)
    private(set) var lastBakeDurationMs: Double = 0.0

    // Interactive World Map Controller state
    private(set) var isWorldMapOpen: Bool = false
    private(set) var mapCenterNorm: SIMD2<Float> = SIMD2<Float>(0.5, 0.5)
    private(set) var mapZoom: Float = 1.25
    private(set) var selectedMarkerIndex: Int = 0
    private(set) var mapMarkers: [RTWorldMapMarker] = []

    var isEventActiveOrCountdown: Bool {
        eventState == .countdown || eventState == .active
    }

    var isBossDuelActive: Bool {
        guard let ev = activeEvent, ev.kind == .boss else { return false }
        return eventState == .countdown || eventState == .active
    }

    var bossColorRGB: SIMD3<Float> {
        activeEvent?.bossPaintRGB ?? SIMD3<Float>(0.90, 0.16, 0.20)
    }

    // MARK: - Initialization & Graceful JSON Loader

    init(eventsJSONURL: URL? = nil, landmarks: [RkptLandmark] = []) {
        RTEventWorldFX.assertLayout()
        self.landmarks = landmarks
        if let url = eventsJSONURL ?? Self.defaultEventsJSONURL() {
            _ = loadEvents(from: url)
        }
        rebuildMapMarkers()
    }

    static func defaultEventsJSONURL() -> URL? {
        let candidates = [
            "build/gameplay_pack/events.json",
            "../build/gameplay_pack/events.json",
            "gameplay_pack/events.json"
        ]
        for path in candidates {
            let url = URL(fileURLWithPath: path)
            if FileManager.default.fileExists(atPath: url.path) {
                return url
            }
        }
        return nil
    }

    /// Loads events from `build/gameplay_pack/events.json`. Returns false gracefully if absent/malformed.
    @discardableResult
    func loadEvents(from url: URL) -> Bool {
        guard FileManager.default.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url),
              let raw = try? JSONDecoder().decode(RawEventsCatalogJSON.self, from: data) else {
            return false
        }

        if let world = raw.world {
            if let bMin = world.roadBoundsMinXZ, bMin.count >= 2,
               let bMax = world.roadBoundsMaxXZ, bMax.count >= 2 {
                worldBoundsMinMax = Self.computeSquareMapBounds(
                    minX: bMin[0], minZ: bMin[1], maxX: bMax[0], maxZ: bMax[1]
                )
            }
            if landmarks.isEmpty, let rawLms = world.landmarks {
                landmarks = rawLms.compactMap { lm in
                    guard lm.spawn_pos.count >= 3 else { return nil }
                    return RkptLandmark(
                        name: lm.name,
                        spawnPos: SIMD3<Float>(lm.spawn_pos[0], lm.spawn_pos[1], lm.spawn_pos[2]),
                        spawnYawRad: lm.spawn_yaw_rad ?? 0.0,
                        regionId: UInt32(max(0, lm.region_id ?? 0)),
                        flags: 0
                    )
                }
            }
        }

        var parsed: [RTEventDefinition] = []
        var byId: [String: RTEventDefinition] = [:]

        for item in raw.events ?? [] {
            guard let kind = RTEventKind(rawValue: item.kind.lowercased()),
                  item.startPos.count >= 3,
                  !item.checkpoints.isEmpty else {
                continue
            }

            let startPos = SIMD3<Float>(item.startPos[0], item.startPos[1], item.startPos[2])
            let startYaw = item.startYawRad ?? 0.0

            let cps: [RTEventCheckpoint] = item.checkpoints.compactMap { cp in
                guard cp.pos.count >= 3 else { return nil }
                return RTEventCheckpoint(
                    pos: SIMD3<Float>(cp.pos[0], cp.pos[1], cp.pos[2]),
                    yawRad: cp.yawRad ?? startYaw,
                    radius: max(8.0, cp.radius ?? (kind == .speedtrap ? 18.0 : 15.0)),
                    roadNodeIdx: cp.roadNodeIdx ?? -1,
                    isSpeedtrap: cp.isSpeedtrap ?? (kind == .speedtrap)
                )
            }
            guard !cps.isEmpty else { continue }

            let wps = Self.buildWaypoints(
                rawWaypoints: item.routeWaypoints,
                startPos: startPos,
                startYaw: startYaw,
                checkpoints: cps
            )
            let computedLen = wps.last?.cumDistM ?? 0.0
            let routeLen = max(computedLen, item.routeLengthM ?? computedLen)

            let paint: SIMD3<Float>
            if let rgb = item.bossPaintRGB, rgb.count >= 3 {
                paint = SIMD3<Float>(rgb[0], rgb[1], rgb[2])
            } else {
                paint = (kind == .speedtrap)
                    ? SIMD3<Float>(0.12, 0.88, 1.0)
                    : SIMD3<Float>(1.0, 0.72, 0.14)
            }

            let def = RTEventDefinition(
                id: item.id,
                kind: kind,
                title: item.title,
                trackName: item.trackName ?? item.title,
                district: item.district ?? "Rockport City",
                districtIndex: item.districtIndex ?? 0,
                sourceEventId: item.sourceEventId ?? "",
                bossRank: item.bossRank ?? 0,
                bossName: item.bossName ?? "",
                bossFullName: item.bossFullName ?? "",
                bossCarKey: item.bossCarKey ?? "",
                bossCarName: item.bossCarName ?? "",
                bossUpgradeMultiplier: max(0.85, item.bossUpgradeMultiplier ?? 1.0),
                bossPaintRGB: paint,
                bountyReward: max(0, item.bountyReward ?? 5000),
                targetScore: item.targetScore ?? (item.parTimeSec ?? 120.0),
                parTimeSec: max(15.0, item.parTimeSec ?? 120.0),
                startPos: startPos,
                startYawRad: startYaw,
                routeLengthM: max(100.0, routeLen),
                checkpoints: cps,
                routeWaypoints: wps
            )
            parsed.append(def)
            byId[def.id.lowercased()] = def
        }

        self.events = parsed
        self.eventsById = byId
        self.isCatalogLoaded = !parsed.isEmpty
        rebuildMapMarkers()
        return self.isCatalogLoaded
    }

    /// Injects synthetic or programmatic events (helpful for unit tests or custom challenges).
    func setEvents(_ defs: [RTEventDefinition], landmarks: [RkptLandmark]? = nil) {
        self.events = defs
        var map: [String: RTEventDefinition] = [:]
        for d in defs {
            map[d.id.lowercased()] = d
        }
        self.eventsById = map
        self.isCatalogLoaded = !defs.isEmpty
        if let lms = landmarks {
            self.landmarks = lms
        }
        rebuildMapMarkers()
    }

    func setLandmarks(_ lms: [RkptLandmark]) {
        self.landmarks = lms
        rebuildMapMarkers()
    }

    private static func buildWaypoints(
        rawWaypoints: [[Float]]?,
        startPos: SIMD3<Float>,
        startYaw: Float,
        checkpoints: [RTEventCheckpoint]
    ) -> [RTEventWaypoint] {
        var pts: [(SIMD3<Float>, Float)] = []
        if let raw = rawWaypoints, raw.count >= 2 {
            pts.reserveCapacity(raw.count)
            for row in raw where row.count >= 3 {
                let p = SIMD3<Float>(row[0], row[1], row[2])
                let y = row.count >= 4 ? row[3] : startYaw
                pts.append((p, y))
            }
        }
        if pts.count < 2 {
            pts = [(startPos, startYaw)]
            for cp in checkpoints {
                pts.append((cp.pos, cp.yawRad))
            }
        }
        var out: [RTEventWaypoint] = []
        out.reserveCapacity(pts.count)
        var cum: Float = 0.0
        for i in 0..<pts.count {
            if i > 0 {
                let d = hypot(pts[i].0.x - pts[i - 1].0.x, pts[i].0.z - pts[i - 1].0.z)
                cum += d
            }
            var yaw = pts[i].1
            if i + 1 < pts.count {
                let dx = pts[i + 1].0.x - pts[i].0.x
                let dz = pts[i + 1].0.z - pts[i].0.z
                if hypot(dx, dz) > 0.5 {
                    yaw = atan2(dx, dz)
                }
            }
            out.append(RTEventWaypoint(pos: pts[i].0, yawRad: yaw, cumDistM: cum))
        }
        return out
    }

    // MARK: - Event Lookup & Launch Helpers

    func findEvent(matching query: String) -> RTEventDefinition? {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if q.isEmpty { return events.first }
        if let exact = eventsById[q] { return exact }

        // Handle shorthand aliases: boss15 -> boss_15, boss1 -> boss_01, sprint -> sprint_01, speedtrap -> speedtrap_hwy99
        if q == "sprint" {
            return events.first(where: { $0.kind == .sprint })
        }
        if q == "speedtrap" || q == "hwy99" || q == "highway99" || q == "highway 99" {
            if let hwy = eventsById["speedtrap_hwy99"] { return hwy }
            return events.first(where: { $0.kind == .speedtrap })
        }
        if q == "boss" {
            return eventsById["boss_15"] ?? events.first(where: { $0.kind == .boss })
        }
        if q.hasPrefix("boss") {
            let digits = q.dropFirst(4).trimmingCharacters(in: CharacterSet(charactersIn: "_-# "))
            if let rank = Int(digits), rank >= 1 && rank <= 15 {
                let padded = String(format: "boss_%02d", rank)
                if let b = eventsById[padded] { return b }
            }
        }
        // Match by boss name (e.g. "sonny", "razor", "baron", "bull")
        if let byBoss = events.first(where: { $0.kind == .boss && $0.bossName.lowercased() == q }) {
            return byBoss
        }
        // Substring fallback on id / title / trackName
        return events.first(where: {
            $0.id.lowercased().contains(q)
                || $0.title.lowercased().contains(q)
                || $0.bossName.lowercased().contains(q)
        })
    }

    @discardableResult
    func startEvent(id: String, skipCountdown: Bool = false) -> RTEventStartResult? {
        guard let ev = findEvent(matching: id) else { return nil }
        return activateEvent(ev, skipCountdown: skipCountdown)
    }

    @discardableResult
    func startEvent(matching query: String, skipCountdown: Bool = false) -> RTEventStartResult? {
        guard let ev = findEvent(matching: query) else { return nil }
        return activateEvent(ev, skipCountdown: skipCountdown)
    }

    @discardableResult
    func startNearestEvent(
        to playerPos: SIMD3<Float>,
        kind: RTEventKind? = nil,
        skipCountdown: Bool = false
    ) -> RTEventStartResult? {
        var best: RTEventDefinition? = nil
        var bestDistSq: Float = .greatestFiniteMagnitude
        for ev in events {
            if let k = kind, ev.kind != k { continue }
            let dx = ev.startPos.x - playerPos.x
            let dz = ev.startPos.z - playerPos.z
            let dSq = dx * dx + dz * dz
            if dSq < bestDistSq {
                bestDistSq = dSq
                best = ev
            }
        }
        guard let chosen = best else { return nil }
        return activateEvent(chosen, skipCountdown: skipCountdown)
    }

    func abortEvent() {
        activeEvent = nil
        eventState = .idle
        countdownSec = 0.0
        currentCheckpointIndex = 0
        elapsedSec = 0.0
        currentScore = 0.0
        lastTrapSpeedKmh = 0.0
        nextGateDistM = 0.0
        playerRank = 1
        bossGapMeters = 0.0
        trapSpeedsKmh.removeAll(keepingCapacity: true)
        bossInstanceState = nil
        bossDistAlongRouteM = 0.0
        bossWaypointCursor = 0
        playerDistAlongRouteM = 0.0
    }

    private func activateEvent(_ ev: RTEventDefinition, skipCountdown: Bool) -> RTEventStartResult {
        activeEvent = ev
        eventState = skipCountdown ? .active : .countdown
        countdownSec = skipCountdown ? 0.0 : Self.defaultCountdownSec
        currentCheckpointIndex = 0
        elapsedSec = 0.0
        currentScore = 0.0
        lastTrapSpeedKmh = 0.0
        radarFlash = 0.0
        playerRank = 1
        bossGapMeters = 0.0
        playerDistAlongRouteM = 0.0
        trapSpeedsKmh.removeAll(keepingCapacity: true)
        isWorldMapOpen = false

        if let cp0 = ev.checkpoints.first {
            nextGateDistM = hypot(cp0.pos.x - ev.startPos.x, cp0.pos.z - ev.startPos.z)
        } else {
            nextGateDistM = 0.0
        }

        if ev.kind == .boss {
            spawnBossOpponent(for: ev, rollingStart: skipCountdown)
        } else {
            bossInstanceState = nil
            bossDistAlongRouteM = 0.0
            bossWaypointCursor = 0
        }

        return RTEventStartResult(event: ev, startPos: ev.startPos, startYawRad: ev.startYawRad)
    }

    private static func bossArchetype(for event: RTEventDefinition) -> RTAIVehicleArchetype {
        // Use sleek sporty archetypes for Blacklist rivals (isCop=false, sirenOn=false so no police siren/flash)
        switch event.bossCarKey.lowercased() {
        case "slr", "db9", "viper", "corvette", "gallardo", "caymans", "supra", "rx8", "eclipsegt":
            return .copSport
        case "mustanggt", "clk500", "lancerevo8":
            return .copGTO
        default:
            return event.bossRank <= 10 ? .copSport : .trafSedan
        }
    }

    private func spawnBossOpponent(for ev: RTEventDefinition, rollingStart: Bool) {
        // Spawn Blacklist Boss visibly ahead-right on the starting grid so duel launches door-to-door!
        let leadDist: Float = rollingStart ? 18.0 : 9.0
        let sample = sampleRoute(ev.routeWaypoints, atDist: leadDist)
        let right = SIMD3<Float>(-cos(sample.yaw), 0.0, sin(sample.yaw))
        let spawnPos = sample.pos + right * 2.4

        var st = RTAIInstanceState(slot: 0)
        st.active = true
        st.archetype = Self.bossArchetype(for: ev)
        st.worldPosition = spawnPos
        st.prevWorldPosition = spawnPos
        st.yawRad = sample.yaw
        st.prevYawRad = sample.yaw
        st.speedMs = rollingStart ? 32.0 : 0.0
        st.velocityWorld = SIMD3<Float>(sin(sample.yaw) * st.speedMs, 0.0, cos(sample.yaw) * st.speedMs)
        st.cruiseSpeedMs = 46.0 * ev.bossUpgradeMultiplier
        st.isCop = false
        st.sirenOn = false
        st.lightbarPhase = 0.0
        st.laneOffsetM = 2.4
        st.hasRoadProbeY = false

        bossInstanceState = st
        bossDistAlongRouteM = leadDist
        bossWaypointCursor = sample.segIdx
        bossGapMeters = playerDistAlongRouteM - bossDistAlongRouteM
        playerRank = (bossGapMeters >= 0.0) ? 1 : 2
    }

    /// Snaps the Blacklist Boss's vertical Y directly onto the GPU raytraced asphalt probe.
    func applyBossRoadProbeHeight(_ roadY: Float, dt: Float, snapImmediate: Bool = false) {
        guard var boss = bossInstanceState, boss.active else { return }
        if roadY > -5000.0 && abs(roadY - boss.worldPosition.y) < 18.0 {
            let alpha: Float = snapImmediate ? 1.0 : min(1.0, max(0.18, dt * 36.0))
            boss.worldPosition.y += (roadY - boss.worldPosition.y) * alpha
            if snapImmediate {
                boss.prevWorldPosition.y = boss.worldPosition.y
            }
            boss.hasRoadProbeY = true
            bossInstanceState = boss
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
        simClockSec += dt

        // Decay 0.45s white radar flash burst cleanly even after finish
        if radarFlash > 0.0 {
            radarFlash = max(0.0, radarFlash - dt / Self.radarFlashDurationSec)
        }

        guard let ev = activeEvent else { return }

        // Handle countdown state
        if eventState == .countdown {
            countdownSec = max(0.0, countdownSec - dt)
            if var boss = bossInstanceState, boss.active {
                boss.prevWorldPosition = boss.worldPosition
                boss.prevYawRad = boss.yawRad
                bossInstanceState = boss
            }
            if let cp = currentActiveCheckpoint() {
                nextGateDistM = hypot(cp.pos.x - playerPos.x, cp.pos.z - playerPos.z)
            }
            if countdownSec <= 0.0 {
                eventState = .active
            }
            return
        }

        guard eventState == .active else {
            // Keep boss coasting smoothly to a stop after finish so it doesn't freeze mid-frame
            if var boss = bossInstanceState, boss.active {
                boss.prevWorldPosition = boss.worldPosition
                boss.prevYawRad = boss.yawRad
                boss.speedMs = max(0.0, boss.speedMs - 22.0 * dt)
                if boss.speedMs > 0.2 {
                    let fwd = SIMD3<Float>(sin(boss.yawRad), 0.0, cos(boss.yawRad))
                    boss.worldPosition += fwd * boss.speedMs * dt
                    boss.wheelSpinRad += (boss.speedMs / 0.31) * dt
                }
                bossInstanceState = boss
            }
            return
        }

        elapsedSec += dt
        let playerSpeedMs = hypot(playerVel.x, playerVel.z)
        let playerSpeedKmh = playerSpeedMs * 3.6

        // Track monotonic player progress along route for Boss gap & chevron guidance
        playerDistAlongRouteM = max(
            playerDistAlongRouteM,
            estimateRouteProgress(ev.routeWaypoints, near: playerPos, hintDist: playerDistAlongRouteM)
        )

        // Advance active Blacklist Boss AI opponent along WRoadNetwork route
        if ev.kind == .boss {
            stepBossOpponent(ev: ev, dt: dt, playerSpeedMs: playerSpeedMs)
            bossGapMeters = playerDistAlongRouteM - bossDistAlongRouteM
            playerRank = (bossGapMeters >= 0.0) ? 1 : 2
        } else {
            playerRank = 1
            bossGapMeters = 0.0
        }

        // Evaluate active checkpoint / speedtrap gate crossing
        if currentCheckpointIndex < ev.checkpoints.count {
            let cp = ev.checkpoints[currentCheckpointIndex]
            let dx = playerPos.x - cp.pos.x
            let dz = playerPos.z - cp.pos.z
            let dist = hypot(dx, dz)
            nextGateDistM = dist

            if dist <= cp.radius {
                if ev.kind == .speedtrap {
                    let clockedKmh = max(1.0, playerSpeedKmh)
                    lastTrapSpeedKmh = clockedKmh
                    trapSpeedsKmh.append(clockedKmh)
                    currentScore += clockedKmh
                    radarFlash = 1.0
                } else {
                    currentScore = elapsedSec
                }

                currentCheckpointIndex += 1

                if currentCheckpointIndex >= ev.checkpoints.count {
                    // Event Finished!
                    nextGateDistM = 0.0
                    switch ev.kind {
                    case .sprint:
                        eventState = .win
                    case .speedtrap:
                        eventState = (currentScore >= ev.targetScore) ? .win : .lose
                    case .boss:
                        let bossAlreadyFinished = (bossDistAlongRouteM >= ev.routeLengthM - 10.0)
                        eventState = bossAlreadyFinished ? .lose : .win
                        playerRank = (eventState == .win) ? 1 : 2
                    }
                    return
                } else {
                    let nxt = ev.checkpoints[currentCheckpointIndex]
                    nextGateDistM = hypot(nxt.pos.x - playerPos.x, nxt.pos.z - playerPos.z)
                }
            }
        }

        // Boss finish check (if Boss crosses finish line while player is still racing)
        if ev.kind == .boss && eventState == .active {
            if bossDistAlongRouteM >= ev.routeLengthM && currentCheckpointIndex < ev.checkpoints.count {
                eventState = .lose
                playerRank = 2
            }
        }
    }

    private func stepBossOpponent(ev: RTEventDefinition, dt: Float, playerSpeedMs: Float) {
        guard var boss = bossInstanceState, boss.active else { return }
        boss.prevWorldPosition = boss.worldPosition
        boss.prevYawRad = boss.yawRad

        let wps = ev.routeWaypoints
        guard wps.count >= 2 else { return }

        // Look ahead 45m & 90m to calculate upcoming corner curvature for authentic braking
        let sNow = sampleRoute(wps, atDist: bossDistAlongRouteM)
        let sMid = sampleRoute(wps, atDist: bossDistAlongRouteM + 45.0)
        let sFar = sampleRoute(wps, atDist: bossDistAlongRouteM + 90.0)

        let turn1 = abs(Self.wrapAngle(sMid.yaw - sNow.yaw))
        let turn2 = abs(Self.wrapAngle(sFar.yaw - sNow.yaw))
        let maxTurn = max(turn1, turn2)

        // Base cruise speed scaled by authentic VLT presetride upgrade tier (1.04x Sonny -> 1.45x Razor)
        let baseTopSpeedMs: Float = 54.0 * ev.bossUpgradeMultiplier // ~202 km/h Sonny -> ~282 km/h Razor
        let cornerFactor: Float = max(0.46, 1.0 - maxTurn * 0.58)
        var targetSpeedMs = baseTopSpeedMs * cornerFactor

        // Fair NFSMW rubber-band pace so duels remain intensely competitive without teleporting
        let gap = playerDistAlongRouteM - bossDistAlongRouteM // + = player ahead
        if gap > 35.0 {
            let boost = min(0.22, (gap - 35.0) / 260.0)
            targetSpeedMs *= (1.0 + boost)
            targetSpeedMs = max(targetSpeedMs, min(baseTopSpeedMs * 1.15, playerSpeedMs * 1.06))
        } else if gap < -40.0 {
            let ease = min(0.18, (-gap - 40.0) / 280.0)
            targetSpeedMs *= (1.0 - ease)
        }
        targetSpeedMs = max(18.0, min(92.0, targetSpeedMs))

        let accel: Float = (targetSpeedMs >= boss.speedMs) ? (11.5 * ev.bossUpgradeMultiplier) : 20.0
        let deltaV = targetSpeedMs - boss.speedMs
        if abs(deltaV) <= accel * dt {
            boss.speedMs = targetSpeedMs
        } else {
            boss.speedMs += (deltaV > 0 ? accel : -accel) * dt
        }

        bossDistAlongRouteM = min(ev.routeLengthM + 25.0, bossDistAlongRouteM + boss.speedMs * dt)
        let nextSample = sampleRoute(wps, atDist: bossDistAlongRouteM)

        // Subtle racing-line lane offset so boss doesn't clip straight into the player's center
        let laneRight = SIMD3<Float>(-cos(nextSample.yaw), 0.0, sin(nextSample.yaw))
        let desiredXZ = nextSample.pos + laneRight * boss.laneOffsetM

        let prevY = boss.worldPosition.y
        let targetY = boss.hasRoadProbeY ? prevY : nextSample.pos.y
        boss.worldPosition = SIMD3<Float>(desiredXZ.x, targetY, desiredXZ.z)

        let yawDiff = Self.wrapAngle(nextSample.yaw - boss.yawRad)
        boss.steerAngleRad = max(-0.45, min(0.45, yawDiff * 1.8))
        boss.yawRad = Self.wrapAngle(boss.yawRad + yawDiff * min(1.0, dt * 10.0))
        boss.velocityWorld = (boss.worldPosition - boss.prevWorldPosition) / max(dt, 1e-4)
        boss.wheelSpinRad += (boss.speedMs / 0.31) * dt
        bossWaypointCursor = nextSample.segIdx

        bossInstanceState = boss
    }

    // MARK: - 3D WorldFX & HUD State Packing

    func currentActiveCheckpoint() -> RTEventCheckpoint? {
        guard let ev = activeEvent, currentCheckpointIndex < ev.checkpoints.count else { return nil }
        return ev.checkpoints[currentCheckpointIndex]
    }

    func currentNextCheckpoint() -> RTEventCheckpoint? {
        guard let ev = activeEvent, currentCheckpointIndex + 1 < ev.checkpoints.count else { return nil }
        return ev.checkpoints[currentCheckpointIndex + 1]
    }

    /// Builds the 128-byte `RTEventWorldFX` uniform for `raytraceKernel [[buffer(16)]]`.
    func currentWorldFX(simTime: Float? = nil) -> RTEventWorldFX {
        let t = simTime ?? simClockSec
        guard let ev = activeEvent, isEventActiveOrCountdown, let cp = currentActiveCheckpoint() else {
            return RTEventWorldFX.inactive(simTime: t, radarFlash: radarFlash)
        }

        var fx = RTEventWorldFX()
        fx.gatePosRadius = SIMD4<Float>(cp.pos.x, cp.pos.y, cp.pos.z, cp.radius)
        let fwd = SIMD3<Float>(sin(cp.yawRad), 0.0, cos(cp.yawRad))
        fx.gateDirKind = SIMD4<Float>(fwd.x, 0.0, fwd.z, ev.kind.fxKindFloat)

        if let nextCp = currentNextCheckpoint() {
            fx.nextPosRadius = SIMD4<Float>(nextCp.pos.x, nextCp.pos.y, nextCp.pos.z, nextCp.radius)
        } else {
            fx.nextPosRadius = .zero
        }

        fx.fxParams = SIMD4<Float>(
            1.0,
            ev.kind == .speedtrap ? 1.0 : 0.0,
            radarFlash,
            t
        )

        // Place up to 4 floating directional road chevrons along WRoadNetwork ahead of player toward gate
        // (road-surface Y; RayTracing.metal adds +1.65m animated hover offset)
        let targetGateDist = estimateRouteProgress(ev.routeWaypoints, near: cp.pos, hintDist: playerDistAlongRouteM)
        let span = max(0.0, targetGateDist - playerDistAlongRouteM)
        if span > 28.0 && ev.routeWaypoints.count >= 2 {
            let offsets: [Float] = [34.0, 68.0, 102.0, 136.0]
            var placed = 0
            for off in offsets {
                let d = playerDistAlongRouteM + off
                if d < targetGateDist - 16.0 && placed < 4 {
                    let s = sampleRoute(ev.routeWaypoints, atDist: d)
                    fx.setChevron(placed, SIMD4<Float>(s.pos.x, s.pos.y, s.pos.z, s.yaw))
                    placed += 1
                }
            }
        }
        return fx
    }

    /// Builds the HUD + Interactive World Map snapshot consumed by `RTRenderer.swift` -> `buffer(5)`.
    func currentHudState(playerPos: SIMD3<Float>? = nil) -> RTEventHudState {
        var hud = RTEventHudState()
        if let ev = activeEvent {
            hud.mode = ev.kind.hudMode
            hud.state = eventState.rawValue
            hud.countdownSec = countdownSec
            hud.cpIndex = UInt32(min(currentCheckpointIndex, ev.checkpoints.count))
            hud.cpTotal = UInt32(ev.checkpoints.count)
            hud.elapsedSec = elapsedSec
            hud.nextGateDistM = nextGateDistM
            hud.targetScore = ev.targetScore
            hud.currentScore = currentScore
            hud.lastTrapSpeedKmh = lastTrapSpeedKmh
            hud.radarFlash = radarFlash
            hud.rank = playerRank
            hud.bossGapM = bossGapMeters
            hud.bossRank = UInt32(max(0, ev.bossRank))
            hud.eventId = ev.id
            hud.eventTitle = ev.title
            hud.bossName = ev.bossName
            hud.bossCarName = ev.bossCarName
            hud.bossColorRGB = ev.bossPaintRGB
        } else {
            hud.radarFlash = radarFlash
        }

        hud.worldMapOpen = isWorldMapOpen
        hud.mapCenterNorm = mapCenterNorm
        hud.mapCenterXZ = mapUVToWorldXZ(mapCenterNorm)
        hud.mapZoom = mapZoom
        hud.worldBoundsMinMax = worldBoundsMinMax
        hud.mapMarkers = mapMarkers

        if !mapMarkers.isEmpty {
            let idx = max(0, min(mapMarkers.count - 1, selectedMarkerIndex))
            hud.selectedMarkerIndex = idx
            hud.selectedMarkerTitle = mapMarkers[idx].title
            hud.selectedMarkerSubtitle = mapMarkers[idx].subtitle
            hud.selectedMarkerKind = mapMarkers[idx].kind
        }

        // Populate up to 16 GPS polyline points (active event if racing; else preview selected event when map is open)
        let routeEvent: RTEventDefinition?
        if let active = activeEvent, isEventActiveOrCountdown {
            routeEvent = active
        } else if isWorldMapOpen,
                  selectedMarkerIndex >= 0,
                  selectedMarkerIndex < mapMarkers.count,
                  let selId = mapMarkers[selectedMarkerIndex].eventId {
            routeEvent = findEvent(matching: selId)
        } else {
            routeEvent = nil
        }

        if let re = routeEvent {
            let (uvPts, xzPts) = sampleGPSPolyline(for: re, maxPoints: Self.maxGPSRoutePoints)
            hud.gpsRoutePointsNorm = uvPts
            hud.gpsRoutePointsXZ = xzPts
        }

        _ = playerPos // reserved for optional future player-relative projection helpers
        return hud
    }

    // MARK: - Interactive Full-Island World Map Controller

    @discardableResult
    func toggleWorldMap(playerPos: SIMD3<Float>? = nil) -> Bool {
        if isWorldMapOpen {
            closeWorldMap()
        } else {
            openWorldMap(playerPos: playerPos)
        }
        return isWorldMapOpen
    }

    func openWorldMap(playerPos: SIMD3<Float>? = nil) {
        isWorldMapOpen = true
        if let p = playerPos {
            let uv = worldXZToMapUV(SIMD2<Float>(p.x, p.z))
            mapCenterNorm = clampMapCenter(uv, zoom: mapZoom)
        }
    }

    func closeWorldMap() {
        isWorldMapOpen = false
    }

    func pan(dx: Float, dy: Float, dt: Float) {
        let clampedDt = max(0.0, min(0.1, dt))
        let speed: Float = 0.55 / max(1.0, mapZoom)
        let next = SIMD2<Float>(
            mapCenterNorm.x + dx * speed * clampedDt,
            mapCenterNorm.y + dy * speed * clampedDt
        )
        mapCenterNorm = clampMapCenter(next, zoom: mapZoom)
    }

    func zoom(delta: Float) {
        mapZoom = max(1.0, min(4.5, mapZoom + delta))
        mapCenterNorm = clampMapCenter(mapCenterNorm, zoom: mapZoom)
    }

    func setZoom(_ newZoom: Float) {
        mapZoom = max(1.0, min(4.5, newZoom))
        mapCenterNorm = clampMapCenter(mapCenterNorm, zoom: mapZoom)
    }

    func cycleSelectedMarker(step: Int) {
        guard !mapMarkers.isEmpty else {
            selectedMarkerIndex = 0
            return
        }
        let n = mapMarkers.count
        selectedMarkerIndex = ((selectedMarkerIndex + step) % n + n) % n
        let m = mapMarkers[selectedMarkerIndex]
        mapCenterNorm = clampMapCenter(m.normUV, zoom: mapZoom)
    }

    func selectMarker(index: Int) {
        guard !mapMarkers.isEmpty else { return }
        selectedMarkerIndex = max(0, min(mapMarkers.count - 1, index))
        mapCenterNorm = clampMapCenter(mapMarkers[selectedMarkerIndex].normUV, zoom: mapZoom)
    }

    /// Confirms the currently selected World Map marker -> teleport to landmark or launch race event!
    func confirmSelection() -> RTWorldMapAction {
        guard !mapMarkers.isEmpty,
              selectedMarkerIndex >= 0,
              selectedMarkerIndex < mapMarkers.count else {
            isWorldMapOpen = false
            return .none
        }
        let marker = mapMarkers[selectedMarkerIndex]
        isWorldMapOpen = false
        if let evId = marker.eventId {
            return .startEvent(id: evId)
        }
        return .teleport(pos: marker.worldPos, yaw: marker.yawRad, label: marker.title)
    }

    private func clampMapCenter(_ uv: SIMD2<Float>, zoom: Float) -> SIMD2<Float> {
        let halfSpan = 0.5 / max(1.0, zoom)
        let lo = min(0.5, halfSpan)
        let hi = max(0.5, 1.0 - halfSpan)
        return SIMD2<Float>(
            max(lo, min(hi, uv.x)),
            max(lo, min(hi, uv.y))
        )
    }

    // MARK: - World <-> Baked 1024x1024 Map UV Coordinate Projection

    static func computeSquareMapBounds(minX: Float, minZ: Float, maxX: Float, maxZ: Float) -> SIMD4<Float> {
        let rawSpanX = max(200.0, maxX - minX)
        let rawSpanZ = max(200.0, maxZ - minZ)
        let span = max(rawSpanX, rawSpanZ) * 1.12
        let cx = 0.5 * (minX + maxX)
        let cz = 0.5 * (minZ + maxZ)
        let half = 0.5 * span
        return SIMD4<Float>(cx - half, cz - half, cx + half, cz + half)
    }

    func worldXZToMapUV(_ xz: SIMD2<Float>) -> SIMD2<Float> {
        let b = worldBoundsMinMax
        let spanX = max(1.0, b.z - b.x)
        let spanZ = max(1.0, b.w - b.y)
        return SIMD2<Float>(
            max(0.0, min(1.0, (xz.x - b.x) / spanX)),
            max(0.0, min(1.0, (xz.y - b.y) / spanZ))
        )
    }

    func mapUVToWorldXZ(_ uv: SIMD2<Float>) -> SIMD2<Float> {
        let b = worldBoundsMinMax
        return SIMD2<Float>(
            b.x + uv.x * (b.z - b.x),
            b.y + uv.y * (b.w - b.y)
        )
    }

    private func sampleGPSPolyline(
        for ev: RTEventDefinition,
        maxPoints: Int
    ) -> ([SIMD2<Float>], [SIMD2<Float>]) {
        let wps = ev.routeWaypoints
        guard !wps.isEmpty else { return ([], []) }
        let cap = max(2, maxPoints)
        var uvs: [SIMD2<Float>] = []
        var xzs: [SIMD2<Float>] = []
        uvs.reserveCapacity(min(cap, wps.count))
        xzs.reserveCapacity(min(cap, wps.count))

        if wps.count <= cap {
            for w in wps {
                let xz = SIMD2<Float>(w.pos.x, w.pos.z)
                xzs.append(xz)
                uvs.append(worldXZToMapUV(xz))
            }
        } else {
            for i in 0..<cap {
                let idx = Int(round(Float(i) * Float(wps.count - 1) / Float(cap - 1)))
                let p = wps[max(0, min(wps.count - 1, idx))].pos
                let xz = SIMD2<Float>(p.x, p.z)
                xzs.append(xz)
                uvs.append(worldXZToMapUV(xz))
            }
        }
        return (uvs, xzs)
    }

    /// Rebuilds the 24 curated World Map markers (Landmarks + Sprints + Speedtraps + Blacklist Bosses).
    private func rebuildMapMarkers() {
        var markers: [RTWorldMapMarker] = []
        markers.reserveCapacity(Self.maxMapMarkers)

        // 1. Up to 6 Blacklist Bosses (Sonny #15, Baron #10, Ming #6, Ronnie #3, Bull #2, Razor #1 — or first 6)
        let bosses = events.filter { $0.kind == .boss }
        let heroRanks = [15, 13, 10, 6, 3, 1]
        var pickedBossIds = Set<String>()
        for r in heroRanks {
            if let b = bosses.first(where: { $0.bossRank == r }), markers.count < Self.maxMapMarkers {
                pickedBossIds.insert(b.id)
                markers.append(makeEventMarker(b))
            }
        }
        for b in bosses where !pickedBossIds.contains(b.id) && markers.count < 6 {
            pickedBossIds.insert(b.id)
            markers.append(makeEventMarker(b))
        }

        // 2. Up to 6 Speedtraps (led by Highway 99)
        let speedtraps = events.filter { $0.kind == .speedtrap }
        for st in speedtraps.prefix(6) where markers.count < Self.maxMapMarkers {
            markers.append(makeEventMarker(st))
        }

        // 3. Up to 6 Sprints (1 per Rockport district)
        let sprints = events.filter { $0.kind == .sprint }
        var seenDist = Set<String>()
        var pickedSprints = Set<String>()
        for sp in sprints where markers.count < 18 {
            if seenDist.insert(sp.district).inserted {
                pickedSprints.insert(sp.id)
                markers.append(makeEventMarker(sp))
            }
        }
        for sp in sprints where !pickedSprints.contains(sp.id) && markers.count < 18 {
            pickedSprints.insert(sp.id)
            markers.append(makeEventMarker(sp))
        }

        // 4. Up to 6 Verified District Landmarks (fast-travel teleport hubs)
        for (i, lm) in landmarks.prefix(6).enumerated() where markers.count < Self.maxMapMarkers {
            let xz = SIMD2<Float>(lm.spawnPos.x, lm.spawnPos.z)
            markers.append(
                RTWorldMapMarker(
                    kind: 0,
                    rank: 0,
                    worldPos: lm.spawnPos,
                    yawRad: lm.spawnYawRad,
                    normUV: worldXZToMapUV(xz),
                    title: lm.name,
                    subtitle: "FAST TRAVEL LANDMARK",
                    eventId: nil,
                    landmarkIndex: i
                )
            )
        }

        self.mapMarkers = markers
        if selectedMarkerIndex >= markers.count {
            selectedMarkerIndex = 0
        }
    }

    private func makeEventMarker(_ ev: RTEventDefinition) -> RTWorldMapMarker {
        let xz = SIMD2<Float>(ev.startPos.x, ev.startPos.z)
        let kmStr = String(format: "%.1f KM", ev.routeLengthM / 1000.0)
        let subtitle: String
        switch ev.kind {
        case .boss:
            subtitle = "BLACKLIST #\(ev.bossRank) • \(ev.bossCarName.uppercased())"
        case .speedtrap:
            subtitle = "SPEEDTRAP • \(ev.checkpoints.count) TRAPS • \(Int(ev.targetScore)) KM/H"
        case .sprint:
            subtitle = "SPRINT • \(ev.district.uppercased()) • \(kmStr)"
        }
        return RTWorldMapMarker(
            kind: ev.kind.markerKind,
            rank: UInt32(max(0, ev.bossRank)),
            worldPos: ev.startPos,
            yawRad: ev.startYawRad,
            normUV: worldXZToMapUV(xz),
            title: ev.title,
            subtitle: subtitle,
            eventId: ev.id,
            landmarkIndex: nil
        )
    }

    // MARK: - O(1) Pre-Baked 1024x1024 Full-Island World Map Texture Generator (< 20 ms CPU)

    /// Creates a 1x1 dark-navy fallback texture so `hudOverlayKernel [[texture(2)]]` is always bound.
    static func makeFallbackWorldMapTexture(device: MTLDevice) -> MTLTexture? {
        let td = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm,
            width: 1,
            height: 1,
            mipmapped: false
        )
        td.usage = [.shaderRead]
        td.storageMode = .shared
        guard let tex = device.makeTexture(descriptor: td) else { return nil }
        var px: [UInt8] = [8, 16, 28, 255]
        tex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: &px, bytesPerRow: 4)
        return tex
    }

    /// Rasterizes ALL road segments (6,538 segments / 229.25 km) into a crisp dark-navy NFSMW
    /// tactical 1024x1024 `.rgba8Unorm` texture in < 20 ms CPU for O(1) GPU World Map rendering.
    @discardableResult
    func bakeWorldMapTexture(
        device: MTLDevice,
        roadNodes: [RkptRoadNode],
        roadSegments: [RkptRoadSegment],
        landmarks: [RkptLandmark]
    ) -> MTLTexture? {
        let t0 = CFAbsoluteTimeGetCurrent()
        var outBounds = worldBoundsMinMax
        let tex = Self.bakeWorldMapTextureStatic(
            device: device,
            roadNodes: roadNodes,
            roadSegments: roadSegments,
            landmarks: landmarks,
            outBoundsMinMax: &outBounds
        )
        lastBakeDurationMs = (CFAbsoluteTimeGetCurrent() - t0) * 1000.0
        if !landmarks.isEmpty {
            self.landmarks = landmarks
        }
        self.worldBoundsMinMax = outBounds
        self.worldMapTexture = tex
        rebuildMapMarkers()
        return tex
    }

    static func bakeWorldMapTexture(
        device: MTLDevice,
        roadNodes: [RkptRoadNode],
        roadSegments: [RkptRoadSegment],
        landmarks: [RkptLandmark]
    ) -> MTLTexture? {
        var dummyBounds = SIMD4<Float>(-2500.0, -2500.0, 2500.0, 2500.0)
        return bakeWorldMapTextureStatic(
            device: device,
            roadNodes: roadNodes,
            roadSegments: roadSegments,
            landmarks: landmarks,
            outBoundsMinMax: &dummyBounds
        )
    }

    private static func bakeWorldMapTextureStatic(
        device: MTLDevice,
        roadNodes: [RkptRoadNode],
        roadSegments: [RkptRoadSegment],
        landmarks: [RkptLandmark],
        outBoundsMinMax: inout SIMD4<Float>
    ) -> MTLTexture? {
        let size = Self.worldMapTextureSize // 1024
        let td = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm,
            width: size,
            height: size,
            mipmapped: false
        )
        td.usage = [.shaderRead]
        td.storageMode = .shared
        guard let tex = device.makeTexture(descriptor: td) else { return nil }

        // 1. Compute aspect-preserving square padded bounds around WRoadNetwork
        if !roadNodes.isEmpty {
            var minX: Float = 1e9, minZ: Float = 1e9
            var maxX: Float = -1e9, maxZ: Float = -1e9
            for n in roadNodes {
                if n.pos.x < minX { minX = n.pos.x }
                if n.pos.z < minZ { minZ = n.pos.z }
                if n.pos.x > maxX { maxX = n.pos.x }
                if n.pos.z > maxZ { maxZ = n.pos.z }
            }
            outBoundsMinMax = computeSquareMapBounds(minX: minX, minZ: minZ, maxX: maxX, maxZ: maxZ)
        } else if !landmarks.isEmpty {
            var minX: Float = 1e9, minZ: Float = 1e9
            var maxX: Float = -1e9, maxZ: Float = -1e9
            for lm in landmarks {
                minX = min(minX, lm.spawnPos.x); minZ = min(minZ, lm.spawnPos.z)
                maxX = max(maxX, lm.spawnPos.x); maxZ = max(maxZ, lm.spawnPos.z)
            }
            outBoundsMinMax = computeSquareMapBounds(minX: minX, minZ: minZ, maxX: maxX, maxZ: maxZ)
        }

        let bMinX = outBoundsMinMax.x
        let bMinZ = outBoundsMinMax.y
        let invSpanX = Float(size - 1) / max(1.0, outBoundsMinMax.z - bMinX)
        let invSpanZ = Float(size - 1) / max(1.0, outBoundsMinMax.w - bMinZ)

        // 2. Build fast 128x128 coarse island landmass & coastline silhouette mask (< 0.4 ms)
        let maskDim = 128
        var maskA = [UInt8](repeating: 0, count: maskDim * maskDim)
        let maskScaleX = Float(maskDim - 1) / max(1.0, outBoundsMinMax.z - bMinX)
        let maskScaleZ = Float(maskDim - 1) / max(1.0, outBoundsMinMax.w - bMinZ)

        maskA.withUnsafeMutableBufferPointer { mBuf in
            guard let mp = mBuf.baseAddress else { return }
            func stampMask(_ wx: Float, _ wz: Float, rad: Int) {
                let cx = Int((wx - bMinX) * maskScaleX)
                let cz = Int((wz - bMinZ) * maskScaleZ)
                let y0 = max(0, cz - rad), y1 = min(maskDim - 1, cz + rad)
                let x0 = max(0, cx - rad), x1 = min(maskDim - 1, cx + rad)
                for y in y0...y1 {
                    let row = y * maskDim
                    for x in x0...x1 {
                        mp[row + x] = 255
                    }
                }
            }
            for n in roadNodes {
                stampMask(n.pos.x, n.pos.z, rad: 2)
            }
            for seg in roadSegments {
                let i0 = Int(seg.node0), i1 = Int(seg.node1)
                if i0 < roadNodes.count && i1 < roadNodes.count {
                    let p0 = roadNodes[i0].pos
                    let p1 = roadNodes[i1].pos
                    stampMask(0.5 * (p0.x + p1.x), 0.5 * (p0.z + p1.z), rad: 2)
                }
            }
        }

        // One 3x3 morphological close/dilate pass + shoreline edge detection on 128x128
        var maskLand = [UInt8](repeating: 0, count: maskDim * maskDim)
        for y in 1..<(maskDim - 1) {
            let row = y * maskDim
            for x in 1..<(maskDim - 1) {
                var hits = 0
                for dy in -1...1 {
                    let r = (y + dy) * maskDim
                    for dx in -1...1 where maskA[r + x + dx] > 0 {
                        hits += 1
                    }
                }
                maskLand[row + x] = (hits >= 3) ? 2 : (hits >= 1 ? 1 : 0) // 2=interior land, 1=coast rim, 0=ocean
            }
        }

        // 3. Allocate 1024x1024 RGBA8 packed UInt32 pixel canvas (4 MB) & paint backdrop + coastline + grid
        @inline(__always)
        func packRGBA(_ r: UInt32, _ g: UInt32, _ b: UInt32, _ a: UInt32 = 255) -> UInt32 {
            return r | (g << 8) | (b << 16) | (a << 24)
        }

        let oceanBase = packRGBA(7, 13, 24)
        let oceanGrid = packRGBA(14, 26, 44)
        let landBase  = packRGBA(16, 26, 42)
        let landGrid  = packRGBA(24, 39, 60)
        let coastCol  = packRGBA(36, 74, 112)

        let aveCasing = packRGBA(22, 46, 72)
        let aveCore   = packRGBA(72, 146, 206)
        let blvdCore  = packRGBA(116, 194, 240)
        let hwyCasing = packRGBA(136, 78, 16)
        let hwyCore   = packRGBA(255, 182, 48)
        let hubRing   = packRGBA(185, 222, 248)

        var pixels = [UInt32](repeating: oceanBase, count: size * size)
        pixels.withUnsafeMutableBufferPointer { pBuf in
            guard let ptr = pBuf.baseAddress else { return }

            // Fill ocean / island landmass / coastline / subtle 64px tactical grid
            for y in 0..<size {
                let rowOff = y * size
                let my = (y >> 3) * maskDim
                let isGridY = ((y & 63) == 0)
                for x in 0..<size {
                    let landState = maskLand[my + (x >> 3)]
                    let isGrid = isGridY || ((x & 63) == 0)
                    if landState == 2 {
                        ptr[rowOff + x] = isGrid ? landGrid : landBase
                    } else if landState == 1 {
                        ptr[rowOff + x] = coastCol
                    } else if isGrid {
                        ptr[rowOff + x] = oceanGrid
                    }
                }
            }

            // Fast integer Bresenham + brush stamp helper (zero allocation, branch-light)
            @inline(__always)
            func drawSegment(_ x0In: Int, _ y0In: Int, _ x1In: Int, _ y1In: Int, color: UInt32, radius: Int) {
                var x0 = x0In
                var y0 = y0In
                let dx = abs(x1In - x0)
                let sx = x0 < x1In ? 1 : -1
                let dy = -abs(y1In - y0)
                let sy = y0 < y1In ? 1 : -1
                var err = dx + dy

                while true {
                    if radius == 0 {
                        if x0 >= 0 && x0 < size && y0 >= 0 && y0 < size {
                            ptr[y0 * size + x0] = color
                        }
                    } else {
                        let py0 = max(0, y0 - radius)
                        let py1 = min(size - 1, y0 + radius)
                        let px0 = max(0, x0 - radius)
                        let px1 = min(size - 1, x0 + radius)
                        for py in py0...py1 {
                            let rOff = py * size
                            for px in px0...px1 {
                                ptr[rOff + px] = color
                            }
                        }
                    }
                    if x0 == x1In && y0 == y1In { break }
                    let e2 = err &* 2
                    if e2 >= dy {
                        err += dy
                        x0 += sx
                    }
                    if e2 <= dx {
                        err += dx
                        y0 += sy
                    }
                }
            }

            // Pre-project all road node pixel coords once (4,385 nodes -> tiny 17 KB cache)
            var nodePxX = [Int](repeating: 0, count: roadNodes.count)
            var nodePxY = [Int](repeating: 0, count: roadNodes.count)
            for i in 0..<roadNodes.count {
                let p = roadNodes[i].pos
                nodePxX[i] = Int((p.x - bMinX) * invSpanX)
                nodePxY[i] = Int((p.z - bMinZ) * invSpanZ)
            }

            // Bucket indices for crisp layered rendering (local -> arterial -> highways on top)
            var localSegs: [Int] = []
            var blvdSegs: [Int] = []
            var hwySegs: [Int] = []
            localSegs.reserveCapacity(roadSegments.count)
            blvdSegs.reserveCapacity(roadSegments.count / 3)
            hwySegs.reserveCapacity(roadSegments.count / 4)

            for (idx, seg) in roadSegments.enumerated() {
                let n0 = Int(seg.node0), n1 = Int(seg.node1)
                guard n0 < roadNodes.count && n1 < roadNodes.count else { continue }
                if seg.isHighway || seg.halfWidthMeters >= 8.0 {
                    hwySegs.append(idx)
                } else if seg.halfWidthMeters >= 6.2 {
                    blvdSegs.append(idx)
                } else {
                    localSegs.append(idx)
                }
            }

            // Pass 1: Local city avenues (casing + crisp steel-blue core)
            for idx in localSegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: aveCasing, radius: 1)
            }
            for idx in localSegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: aveCore, radius: 0)
            }

            // Pass 2: Major boulevards (bright steel-ice blue)
            for idx in blvdSegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: aveCasing, radius: 2)
            }
            for idx in blvdSegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: blvdCore, radius: 1)
            }

            // Pass 3: Highway 99 & Freeways (warm amber glow casing + bright amber-gold core)
            for idx in hwySegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: hwyCasing, radius: 2)
            }
            for idx in hwySegs {
                let s = roadSegments[idx]
                let n0 = Int(s.node0), n1 = Int(s.node1)
                drawSegment(nodePxX[n0], nodePxY[n0], nodePxX[n1], nodePxY[n1], color: hwyCore, radius: 1)
            }

            // Pass 4: Subtle tactical landmark hub rings
            for lm in landmarks {
                let cx = Int((lm.spawnPos.x - bMinX) * invSpanX)
                let cz = Int((lm.spawnPos.z - bMinZ) * invSpanZ)
                let r = 8
                for deg in stride(from: 0, to: 360, by: 6) {
                    let rad = Float(deg) * .pi / 180.0
                    let px = cx + Int(round(cos(rad) * Float(r)))
                    let py = cz + Int(round(sin(rad) * Float(r)))
                    if px >= 0 && px < size && py >= 0 && py < size {
                        ptr[py * size + px] = hubRing
                    }
                }
            }
        }

        pixels.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            tex.replace(
                region: MTLRegionMake2D(0, 0, size, size),
                mipmapLevel: 0,
                withBytes: base,
                bytesPerRow: size * 4
            )
        }
        return tex
    }

    // MARK: - Route Geometry & Math Helpers

    private func sampleRoute(
        _ wps: [RTEventWaypoint],
        atDist rawDist: Float
    ) -> (pos: SIMD3<Float>, yaw: Float, segIdx: Int) {
        guard !wps.isEmpty else { return (.zero, 0.0, 0) }
        if wps.count == 1 || rawDist <= 0.0 {
            return (wps[0].pos, wps[0].yawRad, 0)
        }
        let total = wps[wps.count - 1].cumDistM
        if rawDist >= total {
            let last = wps[wps.count - 1]
            let extra = rawDist - total
            let fwd = SIMD3<Float>(sin(last.yawRad), 0.0, cos(last.yawRad))
            return (last.pos + fwd * extra, last.yawRad, wps.count - 1)
        }

        // Binary search cumulative distance
        var lo = 0
        var hi = wps.count - 1
        while lo + 1 < hi {
            let mid = (lo + hi) >> 1
            if wps[mid].cumDistM <= rawDist {
                lo = mid
            } else {
                hi = mid
            }
        }
        let a = wps[lo]
        let b = wps[lo + 1]
        let span = max(1e-4, b.cumDistM - a.cumDistM)
        let t = max(0.0, min(1.0, (rawDist - a.cumDistM) / span))
        let pos = a.pos + (b.pos - a.pos) * t
        let dx = b.pos.x - a.pos.x
        let dz = b.pos.z - a.pos.z
        let yaw = (hypot(dx, dz) > 0.25) ? atan2(dx, dz) : a.yawRad
        return (pos, yaw, lo)
    }

    private func estimateRouteProgress(
        _ wps: [RTEventWaypoint],
        near pos: SIMD3<Float>,
        hintDist: Float
    ) -> Float {
        guard wps.count >= 2 else { return hintDist }
        var bestDistSq: Float = .greatestFiniteMagnitude
        var bestArc: Float = hintDist

        for i in 0..<(wps.count - 1) {
            let a = wps[i]
            let b = wps[i + 1]
            let abX = b.pos.x - a.pos.x
            let abZ = b.pos.z - a.pos.z
            let lenSq = max(1e-4, abX * abX + abZ * abZ)
            let apX = pos.x - a.pos.x
            let apZ = pos.z - a.pos.z
            let t = max(0.0, min(1.0, (apX * abX + apZ * abZ) / lenSq))
            let px = a.pos.x + abX * t
            let pz = a.pos.z + abZ * t
            let dSq = (px - pos.x) * (px - pos.x) + (pz - pos.z) * (pz - pos.z)
            let candArc = a.cumDistM + (b.cumDistM - a.cumDistM) * t
            // Mild forward preference near hintDist to avoid self-crossing loop jumps
            let penalty: Float = (candArc < hintDist - 80.0 || candArc > hintDist + 450.0) ? 1600.0 : 0.0
            if dSq + penalty < bestDistSq {
                bestDistSq = dSq + penalty
                bestArc = candArc
            }
        }
        return bestArc
    }

    @inline(__always)
    private static func wrapAngle(_ a: Float) -> Float {
        var r = a
        while r > .pi { r -= 2.0 * .pi }
        while r < -.pi { r += 2.0 * .pi }
        return r
    }
}
