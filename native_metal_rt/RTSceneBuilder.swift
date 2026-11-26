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
    var prevModelMatrix: simd_float4x4
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
    let cloudCoverage: Float
}

struct RTLookdevConfig: Codable {
    var preset: String?
    var sun_elevation_deg: Float?
    var sun_azimuth_deg: Float?
    var exposure: Float?
    var cloud_coverage: Float?
    var puddle_wetness: Float?
    var clearcoat_roughness: Float?
    var flake_strength: Float?
    var bloom_intensity: Float?
    var bloom_threshold: Float?
    var denoise_passes: Int?
    var camera_fov: Float?
    var camera_azimuth: Float?
    var camera_elevation: Float?
    var camera_distance: Float?
    var vignette: Float?
    var contrast: Float?
    var saturation: Float?
    var headlights_on: Float?
    var speed_kmh: Float?
    var steer_deg: Float?
}

enum RTPartRole {
    case worldStatic
    case carBody
    case wheelFL
    case wheelFR
    case wheelRL
    case wheelRR
    case brakeFL
    case brakeFR
    case brakeRL
    case brakeRR
    case streetlamp(basePos: SIMD3<Float>)
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
    let role: RTPartRole
    let pivotCenter: SIMD3<Float>
    let sharedBlasIndex: Int?
}

struct RTCarWheelBounds {
    var frontAxleZ: Float
    var rearAxleZ: Float
    var wheelRadiusF: Float
    var wheelRadiusR: Float
    var halfTrackF: Float
    var halfTrackR: Float

    static let defaultM3 = RTCarWheelBounds(
        frontAxleZ: 1.595, rearAxleZ: -1.1295,
        wheelRadiusF: 0.33, wheelRadiusR: 0.33,
        halfTrackF: 0.80, halfTrackR: 0.80
    )
}

struct RTCarCatalogEntry {
    let canonicalName: String
    let alias: String
    let displayName: String
    let carID: UInt32
    let supportsLivery: Bool
}

enum RTCarCatalog {
    static let allCars: [RTCarCatalogEntry] = [
        RTCarCatalogEntry(canonicalName: "BMWM3GTRE46", alias: "m3_gtr", displayName: "BMW M3 GTR E46", carID: 0, supportsLivery: true),
        RTCarCatalogEntry(canonicalName: "CARRERAGT", alias: "carrera_gt", displayName: "Porsche Carrera GT", carID: 1, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "GALLARDO", alias: "gallardo", displayName: "Lamborghini Gallardo", carID: 2, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CORVETTEC6R", alias: "corvette_c6r", displayName: "Chevrolet Corvette C6.R", carID: 3, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "FORDGT", alias: "ford_gt", displayName: "Ford GT", carID: 4, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "MURCIELAGO", alias: "murcielago", displayName: "Lamborghini Murcielago", carID: 5, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "RX7", alias: "rx7", displayName: "Mazda RX-7 (FD3S)", carID: 6, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "SLR", alias: "slr", displayName: "Mercedes-Benz SLR McLaren", carID: 7, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "VIPER", alias: "viper", displayName: "Dodge Viper SRT-10", carID: 8, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "DB9", alias: "db9", displayName: "Aston Martin DB9", carID: 9, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CORVETTE", alias: "corvette", displayName: "Chevrolet Corvette C6", carID: 10, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "911GT2", alias: "911_gt2", displayName: "Porsche 911 GT2", carID: 11, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "911TURBO", alias: "911_turbo", displayName: "Porsche 911 Turbo S", carID: 12, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "997S", alias: "997s", displayName: "Porsche 911 Carrera S", carID: 13, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CAYMANS", alias: "cayman_s", displayName: "Porsche Cayman S", carID: 14, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "MUSTANGGT", alias: "mustang_gt", displayName: "Ford Mustang GT", carID: 15, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "LANCEREVO8", alias: "evo8", displayName: "Mitsubishi Lancer Evo VIII", carID: 16, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "IMPREZAWRX", alias: "wrx_sti", displayName: "Subaru Impreza WRX STi", carID: 17, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "SUPRA", alias: "supra", displayName: "Toyota Supra", carID: 18, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CLK500", alias: "clk500", displayName: "Mercedes-Benz CLK 500", carID: 19, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "SL500", alias: "sl500", displayName: "Mercedes-Benz SL 500", carID: 20, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "SL65", alias: "sl65", displayName: "Mercedes-Benz SL65 AMG", carID: 21, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CAMARO", alias: "camaro", displayName: "Chevrolet Camaro SS", carID: 22, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "GTO", alias: "gto", displayName: "Pontiac GTO", carID: 23, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "MONARO", alias: "monaro", displayName: "Vauxhall Monaro VXR", carID: 24, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "ELISE", alias: "elise", displayName: "Lotus Elise 111R", carID: 25, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "RX8", alias: "rx8", displayName: "Mazda RX-8", carID: 26, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "RX8SPEEDT", alias: "rx8_speedt", displayName: "Mazda RX-8 Mazdaspeed", carID: 27, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "ECLIPSEGT", alias: "eclipse_gt", displayName: "Mitsubishi Eclipse GT", carID: 28, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "IS300", alias: "is300", displayName: "Lexus IS 300", carID: 29, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CTS", alias: "cts", displayName: "Cadillac CTS", carID: 30, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "A3", alias: "a3", displayName: "Audi A3 3.2 Quattro", carID: 31, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "A4", alias: "a4", displayName: "Audi A4 3.2 FSI Quattro", carID: 32, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "TT", alias: "tt", displayName: "Audi TT 3.2 Quattro", carID: 33, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "GTI", alias: "gti", displayName: "VW Golf GTI", carID: 34, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "COBALTSS", alias: "cobalt_ss", displayName: "Chevrolet Cobalt SS", carID: 35, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "CLIO", alias: "clio", displayName: "Renault Clio V6", carID: 36, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "PUNTO", alias: "punto", displayName: "Fiat Grande Punto", carID: 37, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "BMWM3", alias: "bmw_m3", displayName: "BMW M3 GTR Street", carID: 38, supportsLivery: false),
        RTCarCatalogEntry(canonicalName: "BMWM3GTR", alias: "bmw_m3_gtr_race", displayName: "BMW M3 GTR Race", carID: 39, supportsLivery: false),
    ]

    static func resolve(_ raw: String) -> RTCarCatalogEntry {
        let norm = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: "-", with: "_")
            .replacingOccurrences(of: " ", with: "_")
        let compact = norm.replacingOccurrences(of: "_", with: "")
        if norm == "m3" || norm == "bmw" { return allCars[0] }
        if norm == "cgt" || norm == "porsche" { return allCars[1] }
        if norm == "lambo" || norm == "lamborghini" { return allCars[2] }
        if norm == "c6r" { return allCars[3] }
        if norm == "gt" { return allCars[4] }
        if norm == "murci" { return allCars[5] }
        if norm == "evo" { return allCars[16] }
        if norm == "wrx" { return allCars[17] }
        for entry in allCars {
            if entry.canonicalName.lowercased() == norm || entry.canonicalName.lowercased() == compact || entry.alias == norm {
                return entry
            }
        }
        return allCars[0]
    }

    static func index(of canonicalName: String) -> Int {
        return allCars.firstIndex(where: { $0.canonicalName.caseInsensitiveCompare(canonicalName) == .orderedSame }) ?? 0
    }

    static func next(after canonicalName: String, delta: Int = 1) -> RTCarCatalogEntry {
        let idx = index(of: canonicalName)
        let n = allCars.count
        return allCars[((idx + delta) % n + n) % n]
    }

    static func ensureCachedScenePath(for entry: RTCarCatalogEntry) -> String? {
        let fm = FileManager.default
        let cacheScene = "build/car_cache/\(entry.canonicalName)/scene.json"
        // Prefer 58k-tri flagship hq_m3_gtr for M3 GTR and 512px-tex hq_gallardo for Gallardo;
        // prefer smooth-welded car_cache for Carrera GT & all other 40 garage cars!
        if entry.canonicalName == "BMWM3GTRE46" && fm.fileExists(atPath: "build/flywheel/hq_m3_gtr/scene.json") {
            return "build/flywheel/hq_m3_gtr/scene.json"
        }
        if entry.canonicalName == "GALLARDO" && fm.fileExists(atPath: "build/flywheel/hq_gallardo/scene.json") {
            return "build/flywheel/hq_gallardo/scene.json"
        }
        if fm.fileExists(atPath: cacheScene) {
            return cacheScene
        }
        if entry.canonicalName == "CARRERAGT" && fm.fileExists(atPath: "build/flywheel/hq_carrera_gt/scene.json") {
            return "build/flywheel/hq_carrera_gt/scene.json"
        }
        // On-demand ~0.3s ISO extraction via tools/extract_rt_car.py!
        let extractor = "tools/extract_rt_car.py"
        if fm.fileExists(atPath: extractor) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["python3", extractor, "--car", entry.canonicalName]
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus == 0 && fm.fileExists(atPath: cacheScene) {
                    return cacheScene
                }
            } catch {
                // Fall through to legacy paths
            }
        }
        return nil
    }
}

enum RTCarProfile: String, CaseIterable {
    case m3_gtr = "m3_gtr"
    case carrera_gt = "carrera_gt"
    case gallardo = "gallardo"

    static func parse(_ raw: String) -> RTCarProfile {
        switch raw.lowercased().replacingOccurrences(of: "-", with: "_") {
        case "carrera_gt", "carreragt", "porsche", "cgt":
            return .carrera_gt
        case "gallardo", "lamborghini", "lambo":
            return .gallardo
        default:
            return .m3_gtr
        }
    }

    var carID: UInt32 {
        switch self {
        case .m3_gtr: return 0
        case .carrera_gt: return 1
        case .gallardo: return 2
        }
    }

    var displayName: String {
        switch self {
        case .m3_gtr: return "BMW M3 GTR E46"
        case .carrera_gt: return "Porsche Carrera GT"
        case .gallardo: return "Lamborghini Gallardo"
        }
    }

    var defaultScenePath: String {
        switch self {
        case .m3_gtr: return "build/flywheel/hq_m3_gtr/scene.json"
        case .carrera_gt: return "build/flywheel/hq_carrera_gt/scene.json"
        case .gallardo: return "build/flywheel/hq_gallardo/scene.json"
        }
    }

    var supportsLivery: Bool {
        return self == .m3_gtr
    }

    func next() -> RTCarProfile {
        let all = Self.allCases
        let idx = all.firstIndex(of: self) ?? 0
        return all[(idx + 1) % all.count]
    }
}

struct RTScenePackage {
    let sourceLabel: String
    let carProfile: RTCarProfile
    let canonicalCarName: String
    let carAlias: String
    let carDisplayName: String
    let carID: UInt32
    let carSupportsLivery: Bool
    let wheelBounds: RTCarWheelBounds
    let meshes: [RTBLASMesh]
    let vertexBuffer: MTLBuffer
    let indexBuffer: MTLBuffer
    let triMaterialBuffer: MTLBuffer
    let materialBuffer: MTLBuffer
    let asphaltDiffTexture: MTLTexture
    let asphaltNormalTexture: MTLTexture
    let asphaltRoughTexture: MTLTexture
    let concreteDiffTexture: MTLTexture
    let concreteNormalTexture: MTLTexture
    let carbonDiffTexture: MTLTexture
    let carbonNormalTexture: MTLTexture
    let carLiveryMaskTexture: MTLTexture
    let carMiscTexture: MTLTexture
    let carBadgeTexture: MTLTexture
    let carHeadlightTexture: MTLTexture
    let carTaillightTexture: MTLTexture
    let carTireTexture: MTLTexture
    let carInteriorTexture: MTLTexture
    let hasAuthenticCarTextures: Bool
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
    case autumnTerrain = 20
    case coastalWater = 21
    case streetlampLED = 22
    case barrierReflect = 23
    case foliageAutumn = 24
    case foliagePine = 25
    case treeTrunk = 26
    case carPaintSilver = 27
    case carBadge = 28
    case carMiscTrim = 29
    case carMiscChrome = 30
    case carMiscMetal = 31

    static func fromName(_ name: String) -> RTMaterialID {
        switch name {
        case "car_paint_hero": return .carPaint
        case "car_paint_silver": return .carPaintSilver
        case "car_badge": return .carBadge
        case "car_misc_trim": return .carMiscTrim
        case "car_misc_chrome": return .carMiscChrome
        case "car_misc_metal": return .carMiscMetal
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
                name: "Rockport 2005 Late-Autumn Industrial Dusk",
                sunDirection: simd_normalize(SIMD3<Float>(-0.56, 0.20, 0.80)),
                sunColor: SIMD3<Float>(1.18, 0.78, 0.42),
                sunIntensity: 4.8,
                sunConeRadius: 0.026,
                coronaColor: SIMD3<Float>(0.96, 0.56, 0.24),
                skyZenith: SIMD3<Float>(0.055, 0.078, 0.115),
                skyHorizon: SIMD3<Float>(0.58, 0.36, 0.18),
                skyGround: SIMD3<Float>(0.025, 0.024, 0.026),
                iblIntensity: 1.12,
                wetness: 0.60,
                fogDensity: 0.0068,
                exposure: 1.06,
                bloomThreshold: 1.65,
                bloomIntensity: 0.24,
                headlightsOn: 0.88,
                neonTint: SIMD3<Float>(1.0, 0.68, 0.30),
                neonIntensity: 1.15,
                cloudCoverage: 0.76
            ),
            "night_neon": RTLightingPreset(
                key: "night_neon",
                name: "Rockport 2005 Midnight Industrial Rain",
                sunDirection: simd_normalize(SIMD3<Float>(-0.46, 0.34, 0.82)),
                sunColor: SIMD3<Float>(0.48, 0.62, 0.90),
                sunIntensity: 2.35,
                sunConeRadius: 0.042,
                coronaColor: SIMD3<Float>(0.20, 0.36, 0.68),
                skyZenith: SIMD3<Float>(0.012, 0.020, 0.042),
                skyHorizon: SIMD3<Float>(0.058, 0.088, 0.148),
                skyGround: SIMD3<Float>(0.012, 0.015, 0.020),
                iblIntensity: 1.24,
                wetness: 0.92,
                fogDensity: 0.0076,
                exposure: 1.38,
                bloomThreshold: 1.50,
                bloomIntensity: 0.32,
                headlightsOn: 1.40,
                neonTint: SIMD3<Float>(1.0, 0.68, 0.30),
                neonIntensity: 2.35,
                cloudCoverage: 0.72
            ),
            "daylight": RTLightingPreset(
                key: "daylight",
                name: "Rockport 2005 Crisp Autumn Daylight",
                sunDirection: simd_normalize(SIMD3<Float>(0.38, 0.54, 0.75)),
                sunColor: SIMD3<Float>(1.04, 0.98, 0.91),
                sunIntensity: 5.0,
                sunConeRadius: 0.028,
                coronaColor: SIMD3<Float>(0.62, 0.76, 0.92),
                skyZenith: SIMD3<Float>(0.075, 0.20, 0.48),
                skyHorizon: SIMD3<Float>(0.48, 0.62, 0.78),
                skyGround: SIMD3<Float>(0.04, 0.042, 0.045),
                iblIntensity: 1.28,
                wetness: 0.14,
                fogDensity: 0.0042,
                exposure: 1.04,
                bloomThreshold: 1.75,
                bloomIntensity: 0.16,
                headlightsOn: 0.55,
                neonTint: SIMD3<Float>(1.0, 0.72, 0.38),
                neonIntensity: 0.22,
                cloudCoverage: 0.42
            )
        ]
    }

    /// Continuous 24-hour dynamic sky/sun/moon/fog/weather evaluator for live day-night cycling (key `4` & `--time-of-day`).
    static func lightingPreset(forTimeOfDayHours rawHours: Float, rainIntensity rawRain: Float) -> RTLightingPreset {
        let h = ((rawHours.truncatingRemainder(dividingBy: 24.0)) + 24.0).truncatingRemainder(dividingBy: 24.0)
        let rain = max(0.0, min(1.0, rawRain))

        // Solar elevation angle: sunrise ~6:00, solar noon ~13:00, sunset ~19:00
        let solarPhase = (h - 6.0) / 13.0 * Float.pi
        let sunSin = sin(solarPhase)
        let isDay = (h >= 5.2 && h <= 19.6)
        let sunElev = isDay ? max(0.04, sunSin * 0.78) : max(0.18, sin(((h < 5.2 ? h + 24.0 : h) - 19.6) / 9.6 * Float.pi) * 0.52)
        let sunAz = (h / 24.0) * 2.0 * Float.pi - 0.85
        let sunDir = simd_normalize(SIMD3<Float>(cos(sunAz) * cos(sunElev), sunElev, sin(sunAz) * cos(sunElev)))

        // Smooth blends: night (0..5.2, 20.2..24), dawn (5.2..8.2), midday (8.2..15.8), golden hour (15.8..18.8), dusk (18.8..20.2)
        func smoothstep(_ e0: Float, _ e1: Float, _ x: Float) -> Float {
            let t = max(0.0, min(1.0, (x - e0) / max(e1 - e0, 1e-4)))
            return t * t * (3.0 - 2.0 * t)
        }
        let dawnW = smoothstep(5.0, 6.6, h) * (1.0 - smoothstep(7.2, 9.2, h))
        let dayW  = smoothstep(7.5, 10.0, h) * (1.0 - smoothstep(15.2, 17.2, h))
        let goldW = smoothstep(15.5, 17.4, h) * (1.0 - smoothstep(18.5, 19.8, h))
        let duskW = smoothstep(18.2, 19.4, h) * (1.0 - smoothstep(20.0, 21.4, h))
        let nightW = max(0.0, 1.0 - min(1.0, dawnW + dayW + goldW + duskW))
        let wSum = max(dawnW + dayW + goldW + duskW + nightW, 1e-4)
        let wd = dawnW / wSum, wy = dayW / wSum, wg = goldW / wSum, wk = duskW / wSum, wn = nightW / wSum

        func blend3(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ c: SIMD3<Float>, _ d: SIMD3<Float>, _ e: SIMD3<Float>) -> SIMD3<Float> {
            return a * wd + b * wy + c * wg + d * wk + e * wn
        }
        func blend1(_ a: Float, _ b: Float, _ c: Float, _ d: Float, _ e: Float) -> Float {
            return a * wd + b * wy + c * wg + d * wk + e * wn
        }

        var sunCol = blend3(
            SIMD3<Float>(1.12, 0.72, 0.44),  // dawn rose-amber
            SIMD3<Float>(1.04, 0.98, 0.91),  // midday crisp
            SIMD3<Float>(1.18, 0.78, 0.40),  // Rosewood golden hour
            SIMD3<Float>(0.98, 0.48, 0.28),  // dusk ember
            SIMD3<Float>(0.42, 0.54, 0.78)   // moonlight
        )
        var skyZen = blend3(
            SIMD3<Float>(0.055, 0.11, 0.28),
            SIMD3<Float>(0.075, 0.20, 0.48),
            SIMD3<Float>(0.048, 0.105, 0.24),
            SIMD3<Float>(0.025, 0.042, 0.11),
            SIMD3<Float>(0.011, 0.020, 0.044)
        )
        var skyHor = blend3(
            SIMD3<Float>(0.74, 0.46, 0.30),
            SIMD3<Float>(0.48, 0.62, 0.78),
            SIMD3<Float>(0.76, 0.48, 0.26),
            SIMD3<Float>(0.44, 0.24, 0.22),
            SIMD3<Float>(0.048, 0.076, 0.135)
        )
        let corona = blend3(
            SIMD3<Float>(0.95, 0.56, 0.32),
            SIMD3<Float>(0.62, 0.76, 0.92),
            SIMD3<Float>(1.00, 0.62, 0.26),
            SIMD3<Float>(0.65, 0.32, 0.22),
            SIMD3<Float>(0.18, 0.30, 0.52)
        )
        let skyGnd = blend3(
            SIMD3<Float>(0.028, 0.024, 0.024),
            SIMD3<Float>(0.040, 0.042, 0.045),
            SIMD3<Float>(0.028, 0.024, 0.022),
            SIMD3<Float>(0.016, 0.016, 0.022),
            SIMD3<Float>(0.012, 0.015, 0.022)
        )

        var sunInt = blend1(3.4, 5.0, 4.8, 2.2, 1.35)
        var iblInt = blend1(0.95, 1.28, 1.05, 0.85, 0.78)
        let baseWet = blend1(0.45, 0.16, 0.58, 0.68, 0.72)
        let wetness = min(0.96, baseWet + rain * (0.94 - baseWet))
        let fogDensity = blend1(0.0095, 0.0042, 0.0078, 0.0092, 0.0105) + rain * 0.0042
        let exposure = blend1(1.06, 1.04, 1.08, 1.12, 1.16)
        let headlights = max(blend1(0.65, 0.25, 0.70, 1.15, 1.45), rain * 1.05)
        let neonInt = blend1(0.45, 0.10, 0.35, 1.20, 1.85)
        let clouds = min(0.92, blend1(0.50, 0.42, 0.56, 0.62, 0.58) + rain * 0.32)

        // Overcast storm dimming when raining heavily
        if rain > 0.01 {
            let storm = rain * 0.55
            sunInt *= (1.0 - storm * 0.52)
            sunCol = simd_mix(sunCol, SIMD3<Float>(0.62, 0.70, 0.84), SIMD3<Float>(repeating: storm * 0.45))
            skyZen = simd_mix(skyZen, SIMD3<Float>(0.022, 0.032, 0.058), SIMD3<Float>(repeating: storm * 0.55))
            skyHor = simd_mix(skyHor, SIMD3<Float>(0.18, 0.22, 0.28) * max(0.32, 1.0 - wn * 0.7), SIMD3<Float>(repeating: storm * 0.55))
            iblInt *= (1.0 - storm * 0.15)
        }

        return RTLightingPreset(
            key: String(format: "tod_%02d", Int(h)),
            name: String(format: "Rockport 24h (%02d:%02d)", Int(h), Int((h - floor(h)) * 60.0)),
            sunDirection: sunDir,
            sunColor: sunCol,
            sunIntensity: sunInt,
            sunConeRadius: 0.028 + rain * 0.018,
            coronaColor: corona,
            skyZenith: skyZen,
            skyHorizon: skyHor,
            skyGround: skyGnd,
            iblIntensity: iblInt,
            wetness: wetness,
            fogDensity: fogDensity,
            exposure: exposure,
            bloomThreshold: blend1(1.45, 1.75, 1.45, 1.20, 1.05),
            bloomIntensity: blend1(0.28, 0.16, 0.26, 0.38, 0.46),
            headlightsOn: headlights,
            neonTint: SIMD3<Float>(1.0, 0.68, 0.26),
            neonIntensity: neonInt,
            cloudCoverage: clouds
        )
    }

    static func defaultMaterialTable() -> [RTMaterial] {
        var mats = [RTMaterial](repeating: RTMaterial(
            baseColor: SIMD4<Float>(0.7, 0.7, 0.7, 1.0),
            secondaryColor: SIMD4<Float>(0.1, 0.2, 0.5, 1.5),
            pbrParams: SIMD4<Float>(0.5, 0.3, 0.5, 0.05),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        ), count: 32)

        mats[Int(RTMaterialID.carPaint.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.66, 0.69, 0.74, 1.0),
            secondaryColor: SIMD4<Float>(0.015, 0.115, 0.44, 1.56),
            pbrParams: SIMD4<Float>(0.86, 0.18, 1.0, 0.012),
            emissive: SIMD4<Float>(0, 0, 0, 1)
        )
        mats[Int(RTMaterialID.carPaintSilver.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.66, 0.69, 0.74, 1.0),
            secondaryColor: SIMD4<Float>(0.66, 0.69, 0.74, 1.56),
            pbrParams: SIMD4<Float>(0.86, 0.19, 1.0, 0.014),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carBadge.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.80, 0.82, 0.85, 1.0),
            secondaryColor: SIMD4<Float>(0.015, 0.115, 0.44, 1.54),
            pbrParams: SIMD4<Float>(0.35, 0.18, 0.9, 0.02),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carMiscTrim.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.08, 0.085, 0.092, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.48),
            pbrParams: SIMD4<Float>(0.15, 0.48, 0.15, 0.08),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carMiscChrome.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.90, 0.92, 0.95, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.96, 0.08, 0.0, 0.04),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carMiscMetal.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.72, 0.74, 0.78, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.92, 0.22, 0.2, 0.06),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.chrome.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.94, 0.95, 0.97, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(1.0, 0.042, 0.0, 0.04),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brushedAluminum.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.76, 0.78, 0.81, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.94, 0.20, 0.20, 0.06),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.alloyRim.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.88, 0.90, 0.94, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.96, 0.115, 0.55, 0.032),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.matteTrim.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.045, 0.048, 0.054, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.55, 0.0, 0.2),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.carbonFiber.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.065, 0.070, 0.078, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.54),
            pbrParams: SIMD4<Float>(0.10, 0.22, 1.0, 0.022),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.rubberTire.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.052, 0.054, 0.058, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.48),
            pbrParams: SIMD4<Float>(0.0, 0.52, 0.0, 0.4),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.smokedGlass.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.018, 0.022, 0.030, 0.72),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.52),
            pbrParams: SIMD4<Float>(0.02, 0.022, 1.0, 0.015),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.headlightGlass.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.94, 0.97, 1.0, 0.05),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.52),
            pbrParams: SIMD4<Float>(0.0, 0.012, 1.0, 0.012),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.headlightEmit.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.02, 0.024, 0.030, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.75, 0.16, 0.6, 0.02),
            emissive: SIMD4<Float>(4.2, 4.8, 6.2, 0)
        )
        mats[Int(RTMaterialID.taillightEmit.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.14, 0.005, 0.004, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.54),
            pbrParams: SIMD4<Float>(0.04, 0.045, 1.0, 0.014),
            emissive: SIMD4<Float>(5.5, 0.08, 0.03, 0)
        )
        mats[Int(RTMaterialID.interiorDark.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.055, 0.058, 0.064, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.02, 0.72, 0.0, 0.4),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brakeRotor.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.76, 0.78, 0.82, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.94, 0.22, 0.0, 0.1),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.brakeCaliper.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.88, 0.065, 0.045, 1.0),
            secondaryColor: SIMD4<Float>(0.88, 0.065, 0.045, 1.54),
            pbrParams: SIMD4<Float>(0.42, 0.18, 1.0, 0.025),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.licensePlate.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.16, 0.19, 0.24, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.15, 0.36, 0.3, 0.1),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.wetAsphalt.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.12, 0.125, 0.135, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.32, 0.0, 0.03),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.urbanConcrete.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.34, 0.345, 0.355, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.74, 0.0, 0.3),
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
        mats[Int(RTMaterialID.autumnTerrain.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.16, 0.11, 0.05, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.86, 0.0, 0.5),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.coastalWater.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.012, 0.032, 0.048, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.333),
            pbrParams: SIMD4<Float>(0.02, 0.022, 0.9, 0.02),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.streetlampLED.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(1.0, 0.88, 0.68, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.1, 0.15, 0.5, 0.05),
            emissive: SIMD4<Float>(4.5, 3.6, 2.2, 0)
        )
        mats[Int(RTMaterialID.barrierReflect.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.95, 0.55, 0.12, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.5),
            pbrParams: SIMD4<Float>(0.2, 0.12, 0.8, 0.04),
            emissive: SIMD4<Float>(1.6, 0.65, 0.12, 0)
        )
        mats[Int(RTMaterialID.foliageAutumn.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.42, 0.15, 0.03, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.82, 0.0, 0.5),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.foliagePine.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.045, 0.085, 0.040, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.82, 0.0, 0.5),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        mats[Int(RTMaterialID.treeTrunk.rawValue)] = RTMaterial(
            baseColor: SIMD4<Float>(0.09, 0.07, 0.055, 1.0),
            secondaryColor: SIMD4<Float>(0, 0, 0, 1.45),
            pbrParams: SIMD4<Float>(0.0, 0.88, 0.0, 0.5),
            emissive: SIMD4<Float>(0, 0, 0, 0)
        )
        return mats
    }

    static func loadLookdevConfig(from path: String) throws -> RTLookdevConfig {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        return try JSONDecoder().decode(RTLookdevConfig.self, from: data)
    }

    private static func roleForPartName(_ name: String) -> RTPartRole {
        let upper = name.uppercased()
        if upper.contains("BRAKE") {
            if upper.contains("FL") { return .brakeFL }
            if upper.contains("FR") { return .brakeFR }
            if upper.contains("RL") { return .brakeRL }
            if upper.contains("RR") { return .brakeRR }
        }
        if upper.contains("WHEEL") || upper.contains("ENKEI") {
            if upper.contains("FL") { return .wheelFL }
            if upper.contains("FR") { return .wheelFR }
            if upper.contains("RL") { return .wheelRL }
            if upper.contains("RR") { return .wheelRR }
        }
        return .carBody
    }

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

    private static func parseOBJIntoPool(
        url: URL,
        name: String,
        ps2ZUpToYUp: Bool,
        isTransparent: Bool,
        liveryEnabled: Bool,
        modelMatrix: simd_float4x4,
        role: RTPartRole,
        materialMapper: (String, SIMD3<Float>) -> RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) throws -> RTBLASMesh {
        let raw = try String(contentsOf: url, encoding: .utf8)
        var positions: [SIMD3<Float>] = []
        var texCoords: [SIMD2<Float>] = []
        var normals: [SIMD3<Float>] = []
        positions.reserveCapacity(8192)
        texCoords.reserveCapacity(8192)
        normals.reserveCapacity(8192)

        var currentGroup = "car_paint_hero"
        var localVerts: [RTVertex] = []
        var localIndices: [UInt32] = []
        var localTriMats: [UInt32] = []
        var vertCache: [SIMD3<Int32>: UInt32] = [:]
        var minB = SIMD3<Float>(repeating:  Float.greatestFiniteMagnitude)
        var maxB = SIMD3<Float>(repeating: -Float.greatestFiniteMagnitude)

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
            minB = simd_min(minB, p)
            maxB = simd_max(maxB, p)

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

        if normals.isEmpty && !localVerts.isEmpty {
            let posArr = localVerts.map { SIMD3<Float>($0.px, $0.py, $0.pz) }
            let smooth = computeSmoothNormals(positions: posArr, indices: localIndices)
            for i in 0..<localVerts.count {
                localVerts[i].nx = smooth[i].x
                localVerts[i].ny = smooth[i].y
                localVerts[i].nz = smooth[i].z
            }
        }

        let pivot = localVerts.isEmpty ? SIMD3<Float>(repeating: 0) : (minB + maxB) * 0.5

        // Filter out EA PS2 square inner-wheel alpha-billboard quads (matte_trim corner vertices at r ~= 1.414 * tireR)
        // which otherwise protrude as a dark vertical tab beneath the front splitter when front wheels are steered.
        let isWheelRole: Bool
        switch role {
        case .wheelFL, .wheelFR, .wheelRL, .wheelRR: isWheelRole = true
        default: isWheelRole = false
        }
        if isWheelRole && !localTriMats.isEmpty {
            var maxTireRadius: Float = 0.0
            for t in 0..<localTriMats.count {
                let m = localTriMats[t]
                if m == RTMaterialID.rubberTire.rawValue || m == RTMaterialID.alloyRim.rawValue {
                    for k in 0..<3 {
                        let v = localVerts[Int(localIndices[t * 3 + k])]
                        let r = hypot(v.py - pivot.y, v.pz - pivot.z)
                        if r > maxTireRadius { maxTireRadius = r }
                    }
                }
            }
            if maxTireRadius > 0.15 {
                let maxAllowedR = maxTireRadius * 1.07
                var keptIndices: [UInt32] = []
                var keptMats: [UInt32] = []
                keptIndices.reserveCapacity(localIndices.count)
                keptMats.reserveCapacity(localTriMats.count)
                for t in 0..<localTriMats.count {
                    let m = localTriMats[t]
                    let i0 = localIndices[t * 3 + 0]
                    let i1 = localIndices[t * 3 + 1]
                    let i2 = localIndices[t * 3 + 2]
                    if m == RTMaterialID.matteTrim.rawValue {
                        let v0 = localVerts[Int(i0)]
                        let v1 = localVerts[Int(i1)]
                        let v2 = localVerts[Int(i2)]
                        let rMax = max(
                            hypot(v0.py - pivot.y, v0.pz - pivot.z),
                            max(hypot(v1.py - pivot.y, v1.pz - pivot.z), hypot(v2.py - pivot.y, v2.pz - pivot.z))
                        )
                        if rMax > maxAllowedR { continue }
                    }
                    keptIndices.append(i0)
                    keptIndices.append(i1)
                    keptIndices.append(i2)
                    keptMats.append(m)
                }
                localIndices = keptIndices
                localTriMats = keptMats
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
            modelMatrix: modelMatrix,
            role: role,
            pivotCenter: pivot,
            sharedBlasIndex: nil
        )
    }

    private static func appendQuad(
        _ p0: SIMD3<Float>, _ p1: SIMD3<Float>, _ p2: SIMD3<Float>, _ p3: SIMD3<Float>,
        normal: SIMD3<Float>,
        matID: RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32],
        vOffsetBase: Int
    ) {
        let base = UInt32(vertices.count - vOffsetBase)
        let pts = [p0, p1, p2, p3]
        let uvs = [SIMD2<Float>(0, 0), SIMD2<Float>(1, 0), SIMD2<Float>(1, 1), SIMD2<Float>(0, 1)]
        for (k, p) in pts.enumerated() {
            vertices.append(RTVertex(
                px: p.x, py: p.y, pz: p.z,
                nx: normal.x, ny: normal.y, nz: normal.z,
                u: uvs[k].x, v: uvs[k].y,
                mx: p.x, my: p.y, mz: p.z,
                materialID: matID.rawValue
            ))
        }
        indices.append(contentsOf: [base, base + 1, base + 2, base, base + 2, base + 3])
        triMaterials.append(matID.rawValue)
        triMaterials.append(matID.rawValue)
    }

    private static func appendBox(
        center: SIMD3<Float>,
        halfExtents: SIMD3<Float>,
        matID: RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32],
        vOffsetBase: Int = 0
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
        for (n, c) in faces {
            appendQuad(c[0], c[1], c[2], c[3], normal: n, matID: matID,
                       vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOffsetBase)
        }
    }

    private static func appendVerticalTaperedPole(
        baseCenter: SIMD3<Float>,
        topCenter: SIMD3<Float>,
        radiusBase: Float,
        radiusTop: Float,
        segments: Int,
        matID: RTMaterialID,
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32],
        vOffsetBase: Int
    ) {
        let axis = simd_normalize(topCenter - baseCenter)
        let refUp = abs(axis.y) < 0.9 ? SIMD3<Float>(0, 1, 0) : SIMD3<Float>(1, 0, 0)
        let t = simd_normalize(simd_cross(refUp, axis))
        let b = simd_cross(axis, t)

        for i in 0..<segments {
            let a0 = Float(i) / Float(segments) * 2.0 * .pi
            let a1 = Float(i + 1) / Float(segments) * 2.0 * .pi
            let dir0 = t * cos(a0) + b * sin(a0)
            let dir1 = t * cos(a1) + b * sin(a1)

            let p0 = baseCenter + dir0 * radiusBase
            let p1 = baseCenter + dir1 * radiusBase
            let p2 = topCenter + dir1 * radiusTop
            let p3 = topCenter + dir0 * radiusTop

            let base = UInt32(vertices.count - vOffsetBase)
            let pts = [(p0, dir0), (p1, dir1), (p2, dir1), (p3, dir0)]
            for (k, pair) in pts.enumerated() {
                vertices.append(RTVertex(
                    px: pair.0.x, py: pair.0.y, pz: pair.0.z,
                    nx: pair.1.x, ny: pair.1.y, nz: pair.1.z,
                    u: Float(k % 2), v: Float(k / 2),
                    mx: pair.0.x, my: pair.0.y, mz: pair.0.z,
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

    // Build Authentic Gritty Rockport 2005 Industrial Overpass & Safehouse Dockyard:
    // Rain-slicked weathered highway asphalt/concrete deck, reinforced concrete viaduct retaining wall
    // & bridge piers on left, industrial concrete loading dock & corrugated steel warehouse with
    // hanging warm sodium-tungsten pendant worklamps on right (zero cartoon trees or Roblox skyline!).
    private static func appendRockportCoastalHighway(
        meshes: inout [RTBLASMesh],
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        // 1. Rain-Slicked Rockport Industrial Overpass Asphalt Deck (x in [-7.6, +7.6], z in [-155, +155])
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            let hwX: Float = 7.6
            let hwZ: Float = 155.0
            let corners = [
                SIMD3<Float>(-hwX, 0, -hwZ),
                SIMD3<Float>( hwX, 0, -hwZ),
                SIMD3<Float>( hwX, 0,  hwZ),
                SIMD3<Float>(-hwX, 0,  hwZ)
            ]
            for c in corners {
                vertices.append(RTVertex(
                    px: c.x, py: c.y, pz: c.z,
                    nx: 0, ny: 1, nz: 0,
                    u: c.x * 0.25, v: c.z * 0.25,
                    mx: c.x, my: c.y, mz: c.z,
                    materialID: RTMaterialID.wetAsphalt.rawValue
                ))
            }
            indices.append(contentsOf: [0, 2, 1, 0, 3, 2])
            triMaterials.append(RTMaterialID.wetAsphalt.rawValue)
            triMaterials.append(RTMaterialID.wetAsphalt.rawValue)
            meshes.append(RTBLASMesh(
                name: "Rockport_WetIndustrialOverpass",
                vertexOffset: vOff, vertexCount: 4,
                indexOffset: iOff, triOffset: tOff, triangleCount: 2,
                isTransparent: false, flags: 12, modelMatrix: matrix_identity_float4x4,
                role: .worldStatic, pivotCenter: .zero, sharedBlasIndex: nil
            ))
        }

        // 2. Concrete Jersey Parapet, Heavy Concrete Viaduct Piers & Overpass Deck (left),
        //    plus Industrial Concrete Loading Dock & Steel Warehouse Facade (right)
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            let zMin: Float = -155.0
            let zMax: Float = 155.0

            // Left Sloped Reinforced Concrete Jersey Barrier (x ~ -7.0m)
            let jbProfile: [(Float, Float)] = [
                (-7.38, 0.00),
                (-7.34, 0.28),
                (-7.18, 0.92),
                (-6.94, 0.92),
                (-6.78, 0.28),
                (-6.72, 0.00)
            ]
            for k in 0..<(jbProfile.count - 1) {
                let (x0, y0) = jbProfile[k]
                let (x1, y1) = jbProfile[k + 1]
                let segDir = SIMD2<Float>(x1 - x0, y1 - y0)
                let segN = simd_normalize(SIMD3<Float>(-segDir.y, segDir.x, 0.0))
                appendQuad(
                    SIMD3(x0, y0, zMin), SIMD3(x1, y1, zMin),
                    SIMD3(x1, y1, zMax), SIMD3(x0, y0, zMax),
                    normal: segN, matID: .urbanConcrete,
                    vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
                )
            }

            // Left Industrial Concrete Service Walkway + Heavy Concrete Viaduct Piers & Elevated Overpass Beam
            appendQuad(
                SIMD3(-13.5, 0.16, zMin), SIMD3(-7.36, 0.16, zMin),
                SIMD3(-7.36, 0.16, zMax), SIMD3(-13.5, 0.16, zMax),
                normal: SIMD3(0, 1, 0), matID: .urbanConcrete,
                vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            // Outer industrial concrete retaining curb & tubular steel viaduct safety rail on left parapet
            appendBox(center: SIMD3(-7.06, 1.32, 0.0), halfExtents: SIMD3(0.045, 0.035, 155.0),
                      matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
            appendBox(center: SIMD3(-7.06, 1.08, 0.0), halfExtents: SIMD3(0.035, 0.025, 155.0),
                      matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            var railPostZ: Float = -96.0
            while railPostZ <= 120.0 {
                appendBox(center: SIMD3(-7.06, 1.14, railPostZ), halfExtents: SIMD3(0.04, 0.24, 0.04),
                          matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                railPostZ += 6.0
            }

            // Distant heavy reinforced concrete bridge piers & elevated steel-girder overpass along x = -11.5m
            // (framing the gritty Rockport industrial horizon without blocking the dramatic sky!)
            var pierZ: Float = -112.0
            while pierZ <= 112.0 {
                // Concrete pier shaft + capital
                appendBox(center: SIMD3(-11.8, 3.6, pierZ), halfExtents: SIMD3(0.75, 3.6, 0.85),
                          matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                appendBox(center: SIMD3(-11.8, 7.1, pierZ), halfExtents: SIMD3(1.45, 0.45, 1.05),
                          matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                pierZ += 22.0
            }
            // Overhead elevated highway concrete deck & dark weathered steel I-beam fascia (x = -16.5..-9.8, y = 7.55..8.7)
            appendBox(center: SIMD3(-13.2, 8.15, 0.0), halfExtents: SIMD3(3.4, 0.58, 155.0),
                      matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
            appendBox(center: SIMD3(-9.75, 7.72, 0.0), halfExtents: SIMD3(0.14, 0.28, 155.0),
                      matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            // Right Side: Iconic Rockport Industrial Safehouse Concrete Loading Dock & Corrugated Steel Warehouse Bay
            // Concrete shoulder curb + raised loading dock platform (x = +6.1..+10.8m)
            appendQuad(
                SIMD3(6.1, 0.0, zMin), SIMD3(6.45, 0.18, zMin),
                SIMD3(6.45, 0.18, zMax), SIMD3(6.1, 0.0, zMax),
                normal: simd_normalize(SIMD3(-0.38, 0.92, 0.0)), matID: .urbanConcrete,
                vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            appendQuad(
                SIMD3(6.45, 0.18, zMin), SIMD3(7.55, 0.18, zMin),
                SIMD3(7.55, 0.18, zMax), SIMD3(6.45, 0.18, zMax),
                normal: SIMD3(0, 1, 0), matID: .urbanConcrete,
                vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            // Concrete loading dock wall (x = +7.55..+11.2m, y = 0.18..1.28m)
            appendBox(center: SIMD3(9.4, 0.72, 0.0), halfExtents: SIMD3(1.85, 0.56, 155.0),
                      matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
            // Galvanized steel loading dock edge bumper strip
            appendBox(center: SIMD3(7.52, 1.22, 0.0), halfExtents: SIMD3(0.06, 0.07, 155.0),
                      matID: .brushedAluminum, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            // Corrugated dark industrial warehouse wall (x = +10.8m, y = 1.28..9.6m)
            appendBox(center: SIMD3(11.1, 5.45, 0.0), halfExtents: SIMD3(0.32, 4.20, 155.0),
                      matID: .urbanConcrete, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            // Cantilevered dark steel industrial loading-bay roof canopy overhanging dock (x = +6.9..+11.0, y = 5.45)
            appendBox(center: SIMD3(8.95, 5.55, 0.0), halfExtents: SIMD3(2.15, 0.14, 155.0),
                      matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            // Rhythmic dark industrial steel I-columns, diagonal canopy braces & glowing warm warehouse clerestory windows
            var bayZ: Float = -104.0
            while bayZ <= 128.0 {
                // Structural steel I-beam pillar along loading dock front edge
                appendBox(center: SIMD3(7.75, 3.38, bayZ), halfExtents: SIMD3(0.14, 2.15, 0.14),
                          matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                // Cross-beam under canopy
                appendBox(center: SIMD3(9.25, 5.36, bayZ), halfExtents: SIMD3(1.85, 0.10, 0.10),
                          matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                // Industrial metal warehouse window bay between columns
                appendBox(center: SIMD3(10.72, 3.65, bayZ + 4.5), halfExtents: SIMD3(0.05, 0.72, 1.95),
                          matID: .urbanGlass, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                // Hanging warm tungsten-sodium industrial pendant worklamp under canopy!
                appendVerticalTaperedPole(
                    baseCenter: SIMD3(7.45, 5.42, bayZ + 4.5), topCenter: SIMD3(7.45, 4.92, bayZ + 4.5),
                    radiusBase: 0.02, radiusTop: 0.02, segments: 6,
                    matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
                )
                appendVerticalTaperedPole(
                    baseCenter: SIMD3(7.45, 4.96, bayZ + 4.5), topCenter: SIMD3(7.45, 4.76, bayZ + 4.5),
                    radiusBase: 0.08, radiusTop: 0.34, segments: 10,
                    matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
                )
                appendBox(center: SIMD3(7.45, 4.74, bayZ + 4.5), halfExtents: SIMD3(0.18, 0.025, 0.18),
                          matID: .streetlampLED, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                bayZ += 9.0
            }

            // Galvanized W-beam steel safety guardrail along right highway curb (x = +5.98m)
            let wProfile: [(Float, Float)] = [
                (6.05, 0.44), (5.94, 0.50), (5.94, 0.56), (6.01, 0.60),
                (5.94, 0.64), (5.94, 0.70), (6.05, 0.76)
            ]
            for k in 0..<(wProfile.count - 1) {
                let (x0, y0) = wProfile[k]
                let (x1, y1) = wProfile[k + 1]
                let segDir = SIMD2<Float>(x1 - x0, y1 - y0)
                let segN = simd_normalize(SIMD3<Float>(-segDir.y, segDir.x, 0.0))
                appendQuad(
                    SIMD3(x0, y0, zMin), SIMD3(x1, y1, zMin),
                    SIMD3(x1, y1, zMax), SIMD3(x0, y0, zMax),
                    normal: segN, matID: .brushedAluminum,
                    vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
                )
            }
            var gPostZ: Float = -84.0
            while gPostZ <= 124.0 {
                appendBox(center: SIMD3(6.10, 0.39, gPostZ), halfExtents: SIMD3(0.055, 0.39, 0.045),
                          matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
                gPostZ += 4.0
            }

            meshes.append(RTBLASMesh(
                name: "Rockport_IndustrialOverpassStructures",
                vertexOffset: vOff, vertexCount: vertices.count - vOff,
                indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                isTransparent: false, flags: 4, modelMatrix: matrix_identity_float4x4,
                role: .worldStatic, pivotCenter: .zero, sharedBlasIndex: nil
            ))
        }

        // 3. Instanced Weathered Dark Steel Highway Cobra-Head Sodium Streetlamps along right curb (x = +6.35m)
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count

            appendVerticalTaperedPole(
                baseCenter: SIMD3(0, 0, 0), topCenter: SIMD3(0, 6.2, 0),
                radiusBase: 0.11, radiusTop: 0.072, segments: 12,
                matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            appendVerticalTaperedPole(
                baseCenter: SIMD3(0, 6.15, 0), topCenter: SIMD3(-1.05, 6.60, 0),
                radiusBase: 0.068, radiusTop: 0.052, segments: 10,
                matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            appendVerticalTaperedPole(
                baseCenter: SIMD3(-1.02, 6.59, 0), topCenter: SIMD3(-2.15, 6.72, 0),
                radiusBase: 0.052, radiusTop: 0.042, segments: 10,
                matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff
            )
            appendBox(center: SIMD3(-2.22, 6.71, 0), halfExtents: SIMD3(0.32, 0.055, 0.14),
                      matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
            appendBox(center: SIMD3(-2.22, 6.648, 0), halfExtents: SIMD3(0.24, 0.015, 0.10),
                      matID: .streetlampLED, vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

            let protoMeshIndex = meshes.count
            let lampX: Float = 6.35
            let spacing: Float = 16.0
            let startZ: Float = -88.5

            var baseMat = matrix_identity_float4x4
            baseMat.columns.3 = SIMD4<Float>(lampX, 0.0, startZ, 1.0)
            meshes.append(RTBLASMesh(
                name: "Rockport_Streetlamp_0",
                vertexOffset: vOff, vertexCount: vertices.count - vOff,
                indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                isTransparent: false, flags: 4, modelMatrix: baseMat,
                role: .streetlamp(basePos: SIMD3(lampX, 0.0, startZ)),
                pivotCenter: .zero, sharedBlasIndex: nil
            ))

            for k in 1..<12 {
                let z = startZ + Float(k) * spacing
                var instMat = matrix_identity_float4x4
                instMat.columns.3 = SIMD4<Float>(lampX, 0.0, z, 1.0)
                meshes.append(RTBLASMesh(
                    name: "Rockport_Streetlamp_\(k)",
                    vertexOffset: vOff, vertexCount: vertices.count - vOff,
                    indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                    isTransparent: false, flags: 4, modelMatrix: instMat,
                    role: .streetlamp(basePos: SIMD3(lampX, 0.0, z)),
                    pivotCenter: .zero, sharedBlasIndex: protoMeshIndex
                ))
            }
        }
    }

    private static func appendSyntheticCar(
        meshes: inout [RTBLASMesh],
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        do {
            let vOff = vertices.count
            let iOff = indices.count
            let tOff = triMaterials.count
            appendBox(center: SIMD3(0, 0.44, 0.12), halfExtents: SIMD3(0.92, 0.24, 2.22), matID: .carPaint, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(0, 0.84, -0.15), halfExtents: SIMD3(0.72, 0.22, 1.05), matID: .carPaint, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(-0.55, 0.82, -1.88), halfExtents: SIMD3(0.03, 0.18, 0.12), matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.55, 0.82, -1.88), halfExtents: SIMD3(0.03, 0.18, 0.12), matID: .matteTrim, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.0,  1.02, -1.90), halfExtents: SIMD3(0.86, 0.03, 0.18), matID: .carbonFiber, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(-0.58, 0.60, 2.34), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .headlightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.58, 0.60, 2.34), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .headlightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3(-0.56, 0.78, -2.10), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .taillightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
            appendBox(center: SIMD3( 0.56, 0.78, -2.10), halfExtents: SIMD3(0.18, 0.06, 0.03), matID: .taillightEmit, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)

            for idx in iOff..<indices.count { indices[idx] -= UInt32(vOff) }
            meshes.append(RTBLASMesh(
                name: "Synthetic_M3_Body",
                vertexOffset: vOff, vertexCount: vertices.count - vOff,
                indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
                isTransparent: false, flags: 1, modelMatrix: matrix_identity_float4x4,
                role: .carBody, pivotCenter: .zero, sharedBlasIndex: nil
            ))
        }

        let wheelPositions: [(String, SIMD3<Float>, RTPartRole)] = [
            ("Synthetic_Wheel_FL", SIMD3(-0.88, 0.33,  1.58), .wheelFL),
            ("Synthetic_Wheel_FR", SIMD3( 0.88, 0.33,  1.58), .wheelFR),
            ("Synthetic_Wheel_RL", SIMD3(-0.88, 0.33, -1.14), .wheelRL),
            ("Synthetic_Wheel_RR", SIMD3( 0.88, 0.33, -1.14), .wheelRR)
        ]
        for (wName, pos, role) in wheelPositions {
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
                isTransparent: false, flags: 0, modelMatrix: matrix_identity_float4x4,
                role: role, pivotCenter: pos, sharedBlasIndex: nil
            ))
        }
    }

    private static func appendM3WheelWellAndIntakeBackings(
        meshes: inout [RTBLASMesh],
        vertices: inout [RTVertex],
        indices: inout [UInt32],
        triMaterials: inout [UInt32]
    ) {
        let vOff = vertices.count
        let iOff = indices.count
        let tOff = triMaterials.count

        // Dark matte inner wheel-well arch liners tucked inside outer widebody fenders (x=+-0.91) so +12 deg steered front wheels never expose hollow gaps
        // Strictly tucked inside body hull (y >= 0.29, z <= 1.79) so zero dark tab pokes below lower splitter!
        appendBox(center: SIMD3<Float>(-0.63, 0.44,  1.54), halfExtents: SIMD3<Float>(0.11, 0.15, 0.25), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
        appendBox(center: SIMD3<Float>( 0.63, 0.44,  1.54), halfExtents: SIMD3<Float>(0.11, 0.15, 0.25), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
        // Rear inner wheel-well liners
        appendBox(center: SIMD3<Float>(-0.63, 0.36, -1.130), halfExtents: SIMD3<Float>(0.11, 0.24, 0.32), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
        appendBox(center: SIMD3<Float>( 0.63, 0.36, -1.130), halfExtents: SIMD3<Float>(0.11, 0.24, 0.32), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
        // Dark matte radiator / honeycomb backing plate strictly recessed behind curved front bumper openings (zero corner poke-through!)
        appendBox(center: SIMD3<Float>(0.0, 0.29, 1.96), halfExtents: SIMD3<Float>(0.48, 0.09, 0.025), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)
        // Dark underbody floor pan strictly tucked inside lower rocker & front bumper cutout (z in [-1.53..+1.83], |x|<=0.56, y>=0.175)
        appendBox(center: SIMD3<Float>(0.0, 0.20, 0.15), halfExtents: SIMD3<Float>(0.56, 0.025, 1.68), matID: .matteTrim,
                  vertices: &vertices, indices: &indices, triMaterials: &triMaterials, vOffsetBase: vOff)

        meshes.append(RTBLASMesh(
            name: "M3_InnerWheelWellsAndRadiatorBacking",
            vertexOffset: vOff, vertexCount: vertices.count - vOff,
            indexOffset: iOff, triOffset: tOff, triangleCount: triMaterials.count - tOff,
            isTransparent: false, flags: 0, modelMatrix: matrix_identity_float4x4,
            role: .carBody, pivotCenter: .zero, sharedBlasIndex: nil
        ))
    }

    static func buildScene(
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        scenePath: String?,
        carKey: String = "m3_gtr",
        forceSynthetic: Bool,
        skipProceduralWorld: Bool = false
    ) throws -> RTScenePackage {
        var vertices: [RTVertex] = []
        var indices: [UInt32] = []
        var triMaterials: [UInt32] = []
        var meshes: [RTBLASMesh] = []
        vertices.reserveCapacity(80000)
        indices.reserveCapacity(240000)

        let catalogEntry = RTCarCatalog.resolve(carKey)
        var profile = RTCarProfile.parse(carKey)
        var canonicalName = catalogEntry.canonicalName
        var carAlias = catalogEntry.alias
        var displayName = catalogEntry.displayName
        var resolvedCarID = catalogEntry.carID
        var wheelBounds = RTCarWheelBounds.defaultM3
        var sourceLabel = "synthetic"
        var camTarget = SIMD3<Float>(0.0, 0.50, 0.12)
        var camDist: Float = 4.75
        var camAz: Float = 36.0
        var camEl: Float = 7.5
        var camFov: Float = 36.0

        var asphaltPath: String? = "build/cc0_baked/asphalt_02_wet_4k.png"
        let asphaltNormPath = "build/cc0_raw/asphalt_02_pbr/asphalt_02_nor_gl_2k.png"
        let asphaltRoughPath = "build/cc0_raw/asphalt_02_pbr/asphalt_02_rough_2k.png"
        let concreteDiffPath = "build/cc0_raw/highway_concrete/concrete_wall_004_diff_1k.png"
        let concreteNormPath = "build/cc0_raw/highway_concrete/concrete_wall_004_nor_gl_1k.png"
        var carbonPath: String? = "build/cc0_baked/carbon_fiber_4k.png"
        let carbonNormPath = "build/cc0_raw/car_carbon_fiber/Fabric004_1K-PNG_NormalGL.png"

        var carLiveryMaskPath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_livery_mask.png" : nil
        var carMiscPath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_misc.png" : nil
        var carBadgePath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_badging.png" : nil
        var carHeadlightPath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_headlight.png" : nil
        var carTaillightPath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_taillight.png" : nil
        var carTirePath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_tire.png" : nil
        var carInteriorPath: String? = (resolvedCarID == 0) ? "build/flywheel/hq_m3_gtr/textures/m3_interior.png" : nil

        var mats = defaultMaterialTable()
        var carSupportsLivery = catalogEntry.supportsLivery

        let fm = FileManager.default
        let autoCachedScene = (!forceSynthetic && scenePath == nil) ? RTCarCatalog.ensureCachedScenePath(for: catalogEntry) : nil
        let priorityPaths = [
            scenePath,
            autoCachedScene,
            profile.defaultScenePath,
            "build/flywheel/hq_m3_gtr/scene.json",
            "build/metal_wave3/scene/scene.json"
        ].compactMap { $0 }

        var chosenJsonURL: URL? = nil
        if !forceSynthetic {
            for candidate in priorityPaths {
                let u = URL(fileURLWithPath: candidate)
                if candidate.hasSuffix(".json") && fm.fileExists(atPath: u.path) {
                    chosenJsonURL = u
                    break
                } else if fm.fileExists(atPath: u.appendingPathComponent("scene.json").path) {
                    chosenJsonURL = u.appendingPathComponent("scene.json")
                    break
                }
            }

            if let jURL = chosenJsonURL,
               let data = try? Data(contentsOf: jURL),
               let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let parts = root["parts"] as? [[String: Any]] {
                sourceLabel = "scene.json (\(jURL.path))"
                let sceneDir = jURL.deletingLastPathComponent()

                if let cid = root["car_id"] as? String {
                    let resolved = RTCarCatalog.resolve(cid)
                    canonicalName = resolved.canonicalName
                    carAlias = resolved.alias
                    displayName = resolved.displayName
                    resolvedCarID = resolved.carID
                    profile = RTCarProfile.parse(cid)
                    carSupportsLivery = resolved.supportsLivery
                }
                if resolvedCarID != 0 {
                    carLiveryMaskPath = nil
                }
                if let wb = root["wheel_bounds"] as? [String: Any] {
                    wheelBounds = RTCarWheelBounds(
                        frontAxleZ: Float((wb["front_axle_z"] as? Double) ?? 1.40),
                        rearAxleZ: Float((wb["rear_axle_z"] as? Double) ?? -1.30),
                        wheelRadiusF: Float((wb["wheel_radius_f"] as? Double) ?? 0.33),
                        wheelRadiusR: Float((wb["wheel_radius_r"] as? Double) ?? 0.33),
                        halfTrackF: Float((wb["half_track_f"] as? Double) ?? 0.80),
                        halfTrackR: Float((wb["half_track_r"] as? Double) ?? 0.80)
                    )
                }

                // Apply authentic per-car factory paint material from scene.json
                if let matMap = root["materials"] as? [String: Any],
                   let heroPaint = matMap["car_paint_hero"] as? [String: Any] {
                    if let liv = heroPaint["livery_enabled"] as? Bool {
                        carSupportsLivery = liv
                    }
                    if let bc = heroPaint["base_color"] as? [Double], bc.count == 3 {
                        let baseCol = SIMD4<Float>(Float(bc[0]), Float(bc[1]), Float(bc[2]), 1.0)
                        mats[Int(RTMaterialID.carPaint.rawValue)].baseColor = baseCol
                        mats[Int(RTMaterialID.carPaintSilver.rawValue)].baseColor = baseCol
                        mats[Int(RTMaterialID.carPaintSilver.rawValue)].secondaryColor = SIMD4<Float>(baseCol.x, baseCol.y, baseCol.z, 1.56)
                    }
                    if let sc = heroPaint["secondary_color"] as? [Double], sc.count == 3 {
                        let ior = Float((heroPaint["ior"] as? Double) ?? 1.56)
                        mats[Int(RTMaterialID.carPaint.rawValue)].secondaryColor = SIMD4<Float>(Float(sc[0]), Float(sc[1]), Float(sc[2]), ior)
                    }
                    if let met = heroPaint["metallic"] as? Double {
                        mats[Int(RTMaterialID.carPaint.rawValue)].pbrParams.x = Float(met)
                        mats[Int(RTMaterialID.carPaintSilver.rawValue)].pbrParams.x = Float(met)
                    }
                    if let rough = heroPaint["roughness"] as? Double {
                        mats[Int(RTMaterialID.carPaint.rawValue)].pbrParams.y = Float(rough)
                        mats[Int(RTMaterialID.carPaintSilver.rawValue)].pbrParams.y = Float(rough)
                    }
                }

                if let cam = root["camera"] as? [String: Any] {
                    if let t = cam["target"] as? [Double], t.count == 3 {
                        camTarget = SIMD3<Float>(Float(t[0]), Float(t[1]), Float(t[2]))
                    }
                    if let d = cam["distance"] as? Double { camDist = Float(d) }
                    if let a = cam["azimuth_deg"] as? Double { camAz = Float(a) }
                    if let e = cam["elevation_deg"] as? Double { camEl = Float(e) }
                    if let f = cam["fov_deg"] as? Double { camFov = Float(f) }
                }
                // Hero low automotive telephoto framing for authentic NFSMW stance
                camDist = min(camDist, 4.75)
                camEl = min(camEl, 7.6)
                camAz = 36.0
                camFov = 36.0

                if let textures = root["textures"] as? [String: String] {
                    if let a = textures["asphalt_wet"], fm.fileExists(atPath: a) { asphaltPath = a }
                    if let c = textures["carbon_fiber"], fm.fileExists(atPath: c) { carbonPath = c }
                    if let p = textures["car_livery_mask"], fm.fileExists(atPath: p) { carLiveryMaskPath = p }
                    if let p = textures["car_misc"], fm.fileExists(atPath: p) { carMiscPath = p }
                    if let p = textures["car_badging"], fm.fileExists(atPath: p) { carBadgePath = p }
                    if let p = textures["car_headlight"], fm.fileExists(atPath: p) { carHeadlightPath = p }
                    if let p = textures["car_taillight"], fm.fileExists(atPath: p) { carTaillightPath = p }
                    if let p = textures["car_tire"], fm.fileExists(atPath: p) { carTirePath = p }
                    if let p = textures["car_interior"], fm.fileExists(atPath: p) { carInteriorPath = p }
                }

                for part in parts {
                    let visible = (part["visible"] as? Bool) ?? true
                    if !visible { continue }
                    guard let objRel = part["obj"] as? String else { continue }
                    let objURL = sceneDir.appendingPathComponent(objRel)
                    guard fm.fileExists(atPath: objURL.path) else { continue }
                    let pName = (part["name"] as? String) ?? objURL.deletingPathExtension().lastPathComponent
                    let transparent = (part["transparent"] as? Bool) ?? false
                    let role = roleForPartName(pName)
                    let mesh = try parseOBJIntoPool(
                        url: objURL,
                        name: pName,
                        ps2ZUpToYUp: false,
                        isTransparent: transparent,
                        liveryEnabled: carSupportsLivery,
                        modelMatrix: matrix_identity_float4x4,
                        role: role,
                        materialMapper: { matName, _ in RTMaterialID.fromName(matName) },
                        vertices: &vertices,
                        indices: &indices,
                        triMaterials: &triMaterials
                    )
                    if mesh.triangleCount > 0 { meshes.append(mesh) }
                }
                if resolvedCarID == 0 && !meshes.contains(where: { $0.name.uppercased().contains("UNDERBODY_BACKING") }) {
                    appendM3WheelWellAndIntakeBackings(meshes: &meshes, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
                }
                // Auto-derive wheelBounds from loaded wheel meshes if scene.json lacked explicit wheel_bounds
                for m in meshes {
                    switch m.role {
                    case .wheelFL:
                        if abs(m.pivotCenter.z) > 0.2 { wheelBounds.frontAxleZ = m.pivotCenter.z }
                        if abs(m.pivotCenter.x) > 0.2 { wheelBounds.halfTrackF = abs(m.pivotCenter.x) }
                        if m.pivotCenter.y > 0.2 { wheelBounds.wheelRadiusF = m.pivotCenter.y }
                    case .wheelRL:
                        if abs(m.pivotCenter.z) > 0.2 { wheelBounds.rearAxleZ = m.pivotCenter.z }
                        if abs(m.pivotCenter.x) > 0.2 { wheelBounds.halfTrackR = abs(m.pivotCenter.x) }
                        if m.pivotCenter.y > 0.2 { wheelBounds.wheelRadiusR = m.pivotCenter.y }
                    default:
                        break
                    }
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
                    role: .carBody,
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
                    let wheelSlots: [(String, SIMD3<Float>, Float, RTPartRole)] = [
                        ("ENKEI_FL", SIMD3(-0.88, 0.329,  1.595),  1.0, .wheelFL),
                        ("ENKEI_FR", SIMD3( 0.88, 0.329,  1.595), -1.0, .wheelFR),
                        ("ENKEI_RL", SIMD3(-0.88, 0.329, -1.1295), 1.0, .wheelRL),
                        ("ENKEI_RR", SIMD3( 0.88, 0.329, -1.1295),-1.0, .wheelRR)
                    ]
                    for (wName, offset, flipX, wRole) in wheelSlots {
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
                            role: wRole,
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

        if !skipProceduralWorld {
            appendRockportCoastalHighway(meshes: &meshes, vertices: &vertices, indices: &indices, triMaterials: &triMaterials)
        }

        guard let vBuf = device.makeBuffer(bytes: vertices, length: vertices.count * MemoryLayout<RTVertex>.stride, options: .storageModeShared),
              let iBuf = device.makeBuffer(bytes: indices, length: indices.count * MemoryLayout<UInt32>.stride, options: .storageModeShared),
              let tmBuf = device.makeBuffer(bytes: triMaterials, length: triMaterials.count * MemoryLayout<UInt32>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTSceneBuilder", code: 1, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate Metal geometry buffers"])
        }

        // Apply fallback synthetic paint tint if running synthetic mode for non-M3 profiles
        if sourceLabel == "synthetic" {
            if profile == .carrera_gt {
                let col = SIMD4<Float>(0.71, 0.73, 0.76, 1.0)
                mats[Int(RTMaterialID.carPaint.rawValue)].baseColor = col
                mats[Int(RTMaterialID.carPaintSilver.rawValue)].baseColor = col
                carSupportsLivery = false
            } else if profile == .gallardo {
                let col = SIMD4<Float>(0.96, 0.66, 0.025, 1.0)
                mats[Int(RTMaterialID.carPaint.rawValue)].baseColor = col
                mats[Int(RTMaterialID.carPaintSilver.rawValue)].baseColor = col
                carSupportsLivery = false
            }
        }
        guard let mBuf = device.makeBuffer(bytes: mats, length: mats.count * MemoryLayout<RTMaterial>.stride, options: .storageModeShared) else {
            throw NSError(domain: "RTSceneBuilder", code: 2, userInfo: [NSLocalizedDescriptionKey: "Failed to allocate Metal material buffer"])
        }

        let asphaltDiff = asphaltPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 2048) }
            ?? makeProceduralAsphaltTexture(device: device)
        let asphaltNorm = loadTexture(at: URL(fileURLWithPath: asphaltNormPath), device: device, srgb: false, maxDim: 1024)
            ?? makeFlatNormalTexture(device: device)
        let asphaltRough = loadTexture(at: URL(fileURLWithPath: asphaltRoughPath), device: device, srgb: false, maxDim: 1024)
            ?? makeConstantLinearTexture(device: device, value: 115)
        let concreteDiff = loadTexture(at: URL(fileURLWithPath: concreteDiffPath), device: device, srgb: true, maxDim: 1024)
            ?? makeProceduralConcreteTexture(device: device)
        let concreteNorm = loadTexture(at: URL(fileURLWithPath: concreteNormPath), device: device, srgb: false, maxDim: 1024)
            ?? makeFlatNormalTexture(device: device)
        let carbonDiff = carbonPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 1024) }
            ?? makeProceduralCarbonTexture(device: device)
        let carbonNorm = loadTexture(at: URL(fileURLWithPath: carbonNormPath), device: device, srgb: false, maxDim: 1024)
            ?? makeFlatNormalTexture(device: device)

        let loadedLivery = (!forceSynthetic) ? carLiveryMaskPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: false, maxDim: 2048) } : nil
        let loadedMisc = (!forceSynthetic) ? carMiscPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 1024) } : nil
        let loadedBadge = (!forceSynthetic) ? carBadgePath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 512) } : nil
        let loadedHeadlight = (!forceSynthetic) ? carHeadlightPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 512) } : nil
        let loadedTaillight = (!forceSynthetic) ? carTaillightPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 512) } : nil
        let loadedTire = (!forceSynthetic) ? carTirePath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 512) } : nil
        let loadedInterior = (!forceSynthetic) ? carInteriorPath.flatMap { loadTexture(at: URL(fileURLWithPath: $0), device: device, srgb: true, maxDim: 512) } : nil
        let hasAuthenticTex = (loadedMisc != nil || loadedHeadlight != nil || loadedLivery != nil)

        let carLiveryMask = loadedLivery ?? makeConstantLinearTexture(device: device, value: 0)
        let carMisc = loadedMisc ?? makeConstantLinearTexture(device: device, value: 160)
        let carBadge = loadedBadge ?? makeTransparentTexture(device: device)
        let carHeadlight = loadedHeadlight ?? makeConstantLinearTexture(device: device, value: 80)
        let carTaillight = loadedTaillight ?? makeConstantLinearTexture(device: device, value: 80)
        let carTire = loadedTire ?? makeConstantLinearTexture(device: device, value: 120)
        let carInterior = loadedInterior ?? makeConstantLinearTexture(device: device, value: 45)

        return RTScenePackage(
            sourceLabel: sourceLabel,
            carProfile: profile,
            canonicalCarName: canonicalName,
            carAlias: carAlias,
            carDisplayName: displayName,
            carID: resolvedCarID,
            carSupportsLivery: carSupportsLivery && (loadedLivery != nil || forceSynthetic),
            wheelBounds: wheelBounds,
            meshes: meshes,
            vertexBuffer: vBuf,
            indexBuffer: iBuf,
            triMaterialBuffer: tmBuf,
            materialBuffer: mBuf,
            asphaltDiffTexture: asphaltDiff,
            asphaltNormalTexture: asphaltNorm,
            asphaltRoughTexture: asphaltRough,
            concreteDiffTexture: concreteDiff,
            concreteNormalTexture: concreteNorm,
            carbonDiffTexture: carbonDiff,
            carbonNormalTexture: carbonNorm,
            carLiveryMaskTexture: carLiveryMask,
            carMiscTexture: carMisc,
            carBadgeTexture: carBadge,
            carHeadlightTexture: carHeadlight,
            carTaillightTexture: carTaillight,
            carTireTexture: carTire,
            carInteriorTexture: carInterior,
            hasAuthenticCarTextures: hasAuthenticTex,
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

    static func loadTexture(at url: URL, device: MTLDevice, srgb: Bool, maxDim: Int = 1024) -> MTLTexture? {
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
        let cs = srgb ? CGColorSpace(name: CGColorSpace.sRGB) : CGColorSpaceCreateDeviceRGB()
        guard let colorSpace = cs,
              let ctx = CGContext(
                  data: &raw,
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: bytesPerRow,
                  space: colorSpace,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
              ) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        let fmt: MTLPixelFormat = srgb ? .rgba8Unorm_srgb : .rgba8Unorm
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: fmt, width: width, height: height, mipmapped: false)
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
                let v = UInt8(44 + (h % 28))
                let idx = (y * size + x) * 4
                px[idx] = v; px[idx + 1] = v; px[idx + 2] = UInt8(min(255, Int(v) + 2)); px[idx + 3] = 255
            }
        }
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: size, height: size, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, size, size), mipmapLevel: 0, withBytes: px, bytesPerRow: size * 4)
        return tex
    }

    static func makeProceduralConcreteTexture(device: MTLDevice) -> MTLTexture {
        let size = 128
        var px = [UInt8](repeating: 0, count: size * size * 4)
        for y in 0..<size {
            for x in 0..<size {
                let h = ((x * 2654435761) ^ (y * 2246822519)) & 31
                let v = UInt8(122 + Int(h))
                let idx = (y * size + x) * 4
                px[idx] = v; px[idx + 1] = v; px[idx + 2] = UInt8(min(255, Int(v) + 4)); px[idx + 3] = 255
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
                px[idx] = v; px[idx + 1] = v; px[idx + 2] = UInt8(Int(v) + 4); px[idx + 3] = 255
            }
        }
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm_srgb, width: size, height: size, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, size, size), mipmapLevel: 0, withBytes: px, bytesPerRow: size * 4)
        return tex
    }

    static func makeFlatNormalTexture(device: MTLDevice) -> MTLTexture {
        let px: [UInt8] = [128, 128, 255, 255]
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 1, height: 1, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: px, bytesPerRow: 4)
        return tex
    }

    static func makeConstantLinearTexture(device: MTLDevice, value: UInt8) -> MTLTexture {
        let px: [UInt8] = [value, value, value, 255]
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 1, height: 1, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: px, bytesPerRow: 4)
        return tex
    }

    static func makeTransparentTexture(device: MTLDevice) -> MTLTexture {
        let px: [UInt8] = [0, 0, 0, 0]
        let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 1, height: 1, mipmapped: false)
        desc.usage = [.shaderRead]
        let tex = device.makeTexture(descriptor: desc)!
        tex.replace(region: MTLRegionMake2D(0, 0, 1, 1), mipmapLevel: 0, withBytes: px, bytesPerRow: 4)
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
