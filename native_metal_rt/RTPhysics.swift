import Foundation
import GameController
import simd

// MARK: - VLT Vehicle Specification (Decoded from GLOBAL\ATTRIBUTES.BIN via export_gameplay_pack.py)

struct RTVehicleSpec {
    var vltNode: String
    var model: String
    var massKg: Float

    // Engine
    var torqueCurve: [Float]
    var idleRPM: Float
    var redlineRPM: Float
    var maxRPM: Float
    var flywheelMass: Float
    var engineBraking: Float

    // Transmission (gearRatios: [R, N, 1, 2, 3, 4, 5, 6])
    var gearRatios: [Float]
    var finalGear: Float
    var torqueSplit: Float        // 0.0 = RWD, 0.5 = 50/50 AWD
    var shiftSpeed: Float
    var optimalShiftRPM: Float

    // Tires & Handling
    var staticGripFront: Float
    var staticGripRear: Float
    var dynamicGripFront: Float
    var dynamicGripRear: Float
    var gripScaleFront: Float
    var gripScaleRear: Float
    var steering: Float
    var yawControl: [Float]
    var yawSpeed: Float

    // Chassis & Suspension
    var wheelBase: Float
    var trackWidthFront: Float
    var trackWidthRear: Float
    var dragCoefficient: Float
    var aeroCoefficient: Float
    var frontWeightBias: Float
    var swaybarStiffnessFront: Float
    var swaybarStiffnessRear: Float
    var springStiffnessFront: Float
    var springStiffnessRear: Float

    // Brakes
    var brakeFront: Float
    var brakeRear: Float
    var ebrake: Float

    // NOS
    var nosTorqueBoost: Float
    var nosCapacity: Float
    var nosFlowRate: Float
    var nosRechargeMinSec: Float
    var nosRechargeMaxSec: Float

    // Induction (Turbo / Supercharger)
    var turboPSI: Float
    var turboHighBoost: Float
    var turboLowBoost: Float
    var turboSpoolUp: Float
    var turboSpoolDown: Float

    // Audio Binding
    var engineAudioNode: String
    var trannyWhine: Bool

    var forwardGearCount: Int {
        max(1, gearRatios.count - 2)
    }

    func forwardGearRatio(_ gear: Int) -> Float {
        let idx = gear + 1 // 0=R, 1=N, 2=1st...
        guard idx >= 2 && idx < gearRatios.count else { return 1.0 }
        return max(0.4, gearRatios[idx])
    }

    var reverseGearRatio: Float {
        return gearRatios.first.map { max(1.5, abs($0)) } ?? 3.0
    }

    /// Interpolates the 9-point VLT engine torque curve (ft-lb -> Nm scaled) at `rpm`.
    func sampleTorqueNm(at rpm: Float) -> Float {
        guard !torqueCurve.isEmpty else { return 380.0 }
        let span = max(1000.0, maxRPM - idleRPM)
        let u = max(0.0, min(1.0, (rpm - idleRPM) / span))
        let fIdx = u * Float(torqueCurve.count - 1)
        let i0 = Int(floor(fIdx))
        let i1 = min(torqueCurve.count - 1, i0 + 1)
        let frac = fIdx - Float(i0)
        let rawVal = torqueCurve[i0] * (1.0 - frac) + torqueCurve[i1] * frac
        // PS2 NFSMW VLT torque values are calibrated in ft-lb (~467 on M3 GTR); convert to Nm.
        return max(60.0, rawVal * 1.3558)
    }

    static func builtInDefault(for carKey: String) -> RTVehicleSpec {
        let key = carKey.lowercased().replacingOccurrences(of: "-", with: "_")
        switch key {
        case "carrera_gt", "carreragt", "porsche", "cgt":
            return RTVehicleSpec(
                vltNode: "carreragt", model: "CARRERAGT", massKg: 1400.0,
                torqueCurve: [145, 175, 245, 350, 435, 395, 365, 330, 300],
                idleRPM: 800, redlineRPM: 8400, maxRPM: 9200, flywheelMass: 9.0, engineBraking: 0.82,
                gearRatios: [2.19, 0.0, 2.80, 1.87, 1.36, 1.07, 0.90, 0.75],
                finalGear: 4.30, torqueSplit: 0.0, shiftSpeed: 0.22, optimalShiftRPM: 500,
                staticGripFront: 2.25, staticGripRear: 2.35, dynamicGripFront: 2.0, dynamicGripRear: 2.05,
                gripScaleFront: 1.16, gripScaleRear: 1.20, steering: 1.04,
                yawControl: [0.30, 0.65, 1.0, 1.2], yawSpeed: 0.30,
                wheelBase: 2.73, trackWidthFront: 1.61, trackWidthRear: 1.59,
                dragCoefficient: 0.34, aeroCoefficient: 0.32, frontWeightBias: 44.0,
                swaybarStiffnessFront: 240, swaybarStiffnessRear: 245,
                springStiffnessFront: 640, springStiffnessRear: 620,
                brakeFront: 520, brakeRear: 610, ebrake: 910,
                nosTorqueBoost: 1.0, nosCapacity: 5.0, nosFlowRate: 3.0, nosRechargeMinSec: 50, nosRechargeMaxSec: 30,
                turboPSI: 0.0, turboHighBoost: 0.0, turboLowBoost: 0.0, turboSpoolUp: 1.5, turboSpoolDown: 0.3,
                engineAudioNode: "por_car_gt", trannyWhine: false
            )
        case "gallardo", "lamborghini", "lambo":
            return RTVehicleSpec(
                vltNode: "gallardo", model: "GALLARDO", massKg: 1614.0,
                torqueCurve: [140, 195, 265, 335, 376, 365, 345, 320, 295],
                idleRPM: 900, redlineRPM: 8000, maxRPM: 8800, flywheelMass: 10.0, engineBraking: 0.80,
                gearRatios: [2.625, 0.0, 2.563, 1.85, 1.423, 1.138, 0.939, 0.81],
                finalGear: 3.82, torqueSplit: 0.50, shiftSpeed: 0.20, optimalShiftRPM: 450,
                staticGripFront: 2.22, staticGripRear: 2.28, dynamicGripFront: 2.0, dynamicGripRear: 2.02,
                gripScaleFront: 1.14, gripScaleRear: 1.18, steering: 1.02,
                yawControl: [0.32, 0.68, 1.02, 1.22], yawSpeed: 0.32,
                wheelBase: 2.56, trackWidthFront: 1.62, trackWidthRear: 1.59,
                dragCoefficient: 0.35, aeroCoefficient: 0.28, frontWeightBias: 43.0,
                swaybarStiffnessFront: 235, swaybarStiffnessRear: 230,
                springStiffnessFront: 610, springStiffnessRear: 600,
                brakeFront: 500, brakeRear: 580, ebrake: 900,
                nosTorqueBoost: 1.0, nosCapacity: 5.0, nosFlowRate: 3.0, nosRechargeMinSec: 50, nosRechargeMaxSec: 30,
                turboPSI: 10.0, turboHighBoost: 0.18, turboLowBoost: 0.14, turboSpoolUp: 1.6, turboSpoolDown: 0.25,
                engineAudioNode: "lam_gallardo", trannyWhine: false
            )
        default: // BMW M3 GTR E46 (Race)
            return RTVehicleSpec(
                vltNode: "bmwm3gtre46", model: "BMWM3GTRE46", massKg: 1300.0,
                torqueCurve: [170, 251, 340, 428, 467, 452, 411, 375, 350],
                idleRPM: 800, redlineRPM: 8500, maxRPM: 9500, flywheelMass: 10.0, engineBraking: 0.80,
                gearRatios: [3.2, 0.0, 4.10, 2.53, 1.67, 1.23, 1.00, 0.83],
                finalGear: 3.40, torqueSplit: 0.0, shiftSpeed: 0.20, optimalShiftRPM: 500,
                staticGripFront: 2.20, staticGripRear: 2.30, dynamicGripFront: 2.00, dynamicGripRear: 2.00,
                gripScaleFront: 1.175, gripScaleRear: 1.225, steering: 1.05,
                yawControl: [0.30, 0.65, 1.00, 1.20], yawSpeed: 0.30,
                wheelBase: 2.73, trackWidthFront: 1.608, trackWidthRear: 1.603,
                dragCoefficient: 0.32, aeroCoefficient: 0.30, frontWeightBias: 54.0,
                swaybarStiffnessFront: 240, swaybarStiffnessRear: 235,
                springStiffnessFront: 650, springStiffnessRear: 600,
                brakeFront: 495, brakeRear: 615, ebrake: 925,
                nosTorqueBoost: 1.0, nosCapacity: 5.0, nosFlowRate: 3.0, nosRechargeMinSec: 50, nosRechargeMaxSec: 30,
                turboPSI: 12.0, turboHighBoost: 0.20, turboLowBoost: 0.20, turboSpoolUp: 1.8, turboSpoolDown: 0.25,
                engineAudioNode: "tvr_cerb", trannyWhine: true
            )
        }
    }
}

// MARK: - VLT JSON Loader

final class RTVehicleDatabase {
    private var specsByNode: [String: RTVehicleSpec] = [:]
    private var aliases: [String: String] = [:]

    init(jsonURL: URL? = nil) {
        let candidates = [
            jsonURL,
            URL(fileURLWithPath: "build/gameplay_pack/vlt_vehicles.json"),
            URL(fileURLWithPath: "../build/gameplay_pack/vlt_vehicles.json")
        ].compactMap { $0 }

        for url in candidates where FileManager.default.fileExists(atPath: url.path) {
            if load(from: url) { break }
        }
    }

    @discardableResult
    func load(from url: URL) -> Bool {
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return false
        }
        if let al = root["aliases"] as? [String: String] {
            for (k, v) in al { aliases[k.lowercased()] = v.lowercased() }
        }
        guard let vehMap = root["vehicles"] as? [String: [String: Any]] else { return false }

        func fVal(_ d: [String: Any]?, _ k: String, _ def: Float) -> Float {
            if let n = d?[k] as? NSNumber { return n.floatValue }
            return def
        }
        func fArr(_ d: [String: Any]?, _ k: String, _ def: [Float]) -> [Float] {
            if let arr = d?[k] as? [NSNumber], !arr.isEmpty {
                return arr.map { $0.floatValue }
            }
            return def
        }
        func pair(_ d: [String: Any]?, _ k: String, _ defF: Float, _ defR: Float) -> (Float, Float) {
            if let arr = d?[k] as? [NSNumber], arr.count >= 2 {
                return (arr[0].floatValue, arr[1].floatValue)
            }
            return (defF, defR)
        }

        for (nodeKey, raw) in vehMap {
            let eng = raw["engine"] as? [String: Any]
            let trn = raw["transmission"] as? [String: Any]
            let tir = raw["tires"] as? [String: Any]
            let chs = raw["chassis"] as? [String: Any]
            let brk = raw["brakes"] as? [String: Any]
            let nos = raw["nos"] as? [String: Any]
            let ind = raw["induction"] as? [String: Any]
            let aud = raw["audio"] as? [String: Any]

            let sg = pair(tir, "static_grip", 2.0, 2.1)
            let dg = pair(tir, "dynamic_grip", 1.85, 1.90)
            let gs = pair(tir, "grip_scale", 1.08, 1.10)
            let tw = pair(chs, "track_width", 1.58, 1.58)
            let sb = pair(chs, "swaybar_stiffness", 220, 220)
            let sp = pair(chs, "spring_stiffness", 580, 550)
            let bk = pair(brk, "brakes", 460, 550)
            let ebArr = fArr(eng, "engine_braking", [0.8])

            let spec = RTVehicleSpec(
                vltNode: nodeKey.lowercased(),
                model: (raw["model"] as? String) ?? nodeKey.uppercased(),
                massKg: max(750.0, fVal(raw, "mass_kg", 1400.0)),
                torqueCurve: fArr(eng, "torque_curve", [160, 240, 320, 400, 430, 410, 375, 340, 310]),
                idleRPM: fVal(eng, "idle_rpm", 850),
                redlineRPM: fVal(eng, "redline_rpm", 7800),
                maxRPM: fVal(eng, "max_rpm", 8600),
                flywheelMass: max(4.0, fVal(eng, "flywheel_mass", 10.0)),
                engineBraking: ebArr.reduce(0, +) / Float(max(1, ebArr.count)),
                gearRatios: fArr(trn, "gear_ratios", [3.0, 0.0, 3.6, 2.3, 1.6, 1.2, 0.95, 0.78]),
                finalGear: max(2.2, fVal(trn, "final_gear", 3.5)),
                torqueSplit: max(0.0, min(1.0, fVal(trn, "torque_split", 0.0))),
                shiftSpeed: max(0.08, min(0.35, fVal(trn, "shift_speed", 0.20))),
                optimalShiftRPM: fVal(trn, "optimal_shift", 500),
                staticGripFront: sg.0, staticGripRear: sg.1,
                dynamicGripFront: dg.0, dynamicGripRear: dg.1,
                gripScaleFront: gs.0, gripScaleRear: gs.1,
                steering: max(0.7, min(1.5, fVal(tir, "steering", 1.0))),
                yawControl: fArr(tir, "yaw_control", [0.3, 0.65, 1.0, 1.2]),
                yawSpeed: max(0.15, min(0.8, fVal(tir, "yaw_speed", 0.30))),
                wheelBase: max(2.1, min(3.4, fVal(chs, "wheel_base", 2.68))),
                trackWidthFront: tw.0, trackWidthRear: tw.1,
                dragCoefficient: max(0.24, min(0.55, fVal(chs, "drag_coefficient", 0.34))),
                aeroCoefficient: max(0.05, min(0.80, fVal(chs, "aero_coefficient", 0.25))),
                frontWeightBias: fVal(chs, "front_weight_bias", 52.0),
                swaybarStiffnessFront: max(80, sb.0), swaybarStiffnessRear: max(80, sb.1),
                springStiffnessFront: max(250, sp.0), springStiffnessRear: max(250, sp.1),
                brakeFront: bk.0, brakeRear: bk.1, ebrake: fVal(brk, "ebrake", 880),
                nosTorqueBoost: max(0.4, fVal(nos, "torque_boost", 1.0)),
                nosCapacity: max(2.0, fVal(nos, "capacity", 5.0)),
                nosFlowRate: max(1.0, fVal(nos, "flow_rate", 3.0)),
                nosRechargeMinSec: max(10.0, fVal(nos, "recharge_min", 50.0)),
                nosRechargeMaxSec: max(8.0, fVal(nos, "recharge_max", 30.0)),
                turboPSI: fVal(ind, "psi", 0.0),
                turboHighBoost: fVal(ind, "high_boost", 0.0),
                turboLowBoost: fVal(ind, "low_boost", 0.0),
                turboSpoolUp: max(0.3, fVal(ind, "spool_up", 1.6)),
                turboSpoolDown: max(0.1, fVal(ind, "spool_down", 0.25)),
                engineAudioNode: (aud?["engineaudio_node"] as? String) ?? "tvr_cerb",
                trannyWhine: (aud?["tranny_whine"] as? Bool) ?? false
            )
            specsByNode[nodeKey.lowercased()] = spec
        }
        return !specsByNode.isEmpty
    }

    var loadedCount: Int { specsByNode.count }

    func spec(for carKey: String) -> RTVehicleSpec {
        let norm = carKey.lowercased().replacingOccurrences(of: "-", with: "_")
        if let direct = specsByNode[norm] { return direct }
        if let mapped = aliases[norm], let found = specsByNode[mapped] { return found }
        let stripped = norm.replacingOccurrences(of: "_", with: "")
        if let found = specsByNode[stripped] { return found }
        return RTVehicleSpec.builtInDefault(for: carKey)
    }
}

// MARK: - Controls & Native GameController (PS5 DualSense / Xbox)

struct RTDriveInput {
    var throttle: Float = 0.0      // 0..1
    var brake: Float = 0.0         // 0..1
    var steer: Float = 0.0         // -1 (right) .. +1 (left), matches RockportMetalRT convention
    var handbrake: Bool = false
    var nos: Bool = false
    var speedbreaker: Bool = false
}

final class RTGamepadPoller {
    func mergeGamepad(into input: inout RTDriveInput) {
        guard let pad = GCController.controllers().first?.extendedGamepad else { return }

        let gas = pad.rightTrigger.value
        let brk = pad.leftTrigger.value
        if gas > input.throttle { input.throttle = gas }
        if brk > input.brake { input.brake = brk }

        // Left stick X: physical left is -1, physical right is +1; convert to engine steer (+1 left, -1 right)
        let rawX = pad.leftThumbstick.xAxis.value
        if abs(rawX) > 0.08 {
            let mag = (abs(rawX) - 0.08) / 0.92
            let curved = mag * mag * (3.0 - 2.0 * mag) // smooth cubic-like analog response
            let gpSteer = (rawX < 0 ? curved : -curved)
            if abs(gpSteer) > abs(input.steer) {
                input.steer = gpSteer
            }
        }

        if pad.buttonB.isPressed { input.handbrake = true }
        if pad.buttonA.isPressed || pad.rightShoulder.isPressed { input.nos = true }
        if pad.buttonY.isPressed || pad.leftShoulder.isPressed { input.speedbreaker = true }
    }
}

// MARK: - GPU Road/Wall Probe Input & Telemetry Output

struct RTRoadProbeSample {
    var wheelHitY: SIMD4<Float> = SIMD4<Float>(-9999, -9999, -9999, -9999)
    var wallDist: SIMD4<Float> = SIMD4<Float>(999, 999, 999, 999)
    var wallNormalFront: SIMD4<Float> = .zero
    var wallNormalSide: SIMD4<Float> = .zero
    var hasWorld: Bool = false
}

struct RTDriveTelemetry {
    // Transform & Kinematics
    var worldPosition: SIMD3<Float> = .zero
    var velocityWorld: SIMD3<Float> = .zero
    var headingRad: Float = 0.0
    var slipAngleRad: Float = 0.0
    var visualYawRad: Float = 0.0
    var pitchRad: Float = 0.0
    var rollRad: Float = 0.0
    var terrainPitchRad: Float = 0.0
    var terrainRollRad: Float = 0.0
    var steerDeg: Float = 0.0
    var wheelSpinRad: Float = 0.0
    var roadScrollZ: Float = 0.0

    // Drivetrain
    var speedMs: Float = 0.0
    var speedKmh: Float = 0.0
    var rpm: Float = 950.0
    var rpmNorm: Float = 0.14
    var gear: Int = 1                // -1 = R, 0 = N, 1..6
    var isShifting: Bool = false
    var shiftPopTrigger: Bool = false
    var turboBoostNorm: Float = 0.0
    var bovTrigger: Bool = false

    // Drift, Collisions, NOS & Speedbreaker
    var driftIntensity: Float = 0.0
    var isAirborne: Bool = false
    var impactIntensity: Float = 0.0
    var scrapeIntensity: Float = 0.0
    var nosCharge: Float = 1.0       // 0..1
    var nosActive: Bool = false
    var nosFlameIntensity: Float = 0.0
    var nosFovKickDeg: Float = 0.0
    var speedbreakerCharge: Float = 1.0
    var speedbreakerActive: Bool = false
    var speedbreakerBlend: Float = 0.0
    var effectiveTimeScale: Float = 1.0
}

// MARK: - Authentic NFSMW 2005 VLT Arcade-Sim Physics Engine

final class RTPhysicsEngine {
    let database: RTVehicleDatabase
    private(set) var spec: RTVehicleSpec
    private(set) var state: RTDriveTelemetry = RTDriveTelemetry()
    let gamepad = RTGamepadPoller()

    // Internal continuous state (local car frame + spring-damper suspension)
    private var velForward: Float = 0.0
    private var velLateral: Float = 0.0
    private var velVertical: Float = 0.0
    private var yawRate: Float = 0.0
    private var pitchVel: Float = 0.0
    private var rollVel: Float = 0.0
    private var shiftTimer: Float = 0.0
    private var shiftCooldown: Float = 0.0
    private var prevThrottle: Float = 0.0
    private var wasAirborne: Bool = false
    private var backfireDecay: Float = 0.0
    private var smoothSteerInput: Float = 0.0

    init(database: RTVehicleDatabase = RTVehicleDatabase(), carKey: String = "m3_gtr") {
        self.database = database
        self.spec = database.spec(for: carKey)
        reset(position: .zero, headingRad: 0.0)
    }

    func setCar(_ carKey: String) {
        spec = database.spec(for: carKey)
        state.rpm = max(spec.idleRPM, state.rpm)
        state.gear = min(spec.forwardGearCount, max(1, state.gear))
    }

    func reset(position: SIMD3<Float>, headingRad: Float) {
        state = RTDriveTelemetry()
        state.worldPosition = position
        state.headingRad = headingRad
        state.rpm = spec.idleRPM + 120.0
        state.rpmNorm = clamp01((state.rpm - spec.idleRPM) / max(1000.0, spec.redlineRPM - spec.idleRPM))
        state.gear = 0
        state.nosCharge = 1.0
        state.speedbreakerCharge = 1.0
        velForward = 0.0
        velLateral = 0.0
        velVertical = 0.0
        yawRate = 0.0
        pitchVel = 0.0
        rollVel = 0.0
        shiftTimer = 0.0
        shiftCooldown = 0.0
        prevThrottle = 0.0
        wasAirborne = false
        backfireDecay = 0.0
        smoothSteerInput = 0.0
    }

    func configurePose(speedKmh: Float, steerDeg: Float, position: SIMD3<Float>? = nil, headingRad: Float? = nil) {
        if let p = position { state.worldPosition = p }
        if let h = headingRad { state.headingRad = h }
        velForward = max(0.0, min(360.0, speedKmh)) / 3.6
        velLateral = 0.0
        state.speedMs = velForward
        state.speedKmh = velForward * 3.6
        state.steerDeg = max(-28.0, min(28.0, steerDeg))
        updateGearAndRPMFromSpeed(throttle: velForward > 1.0 ? 0.8 : 0.0, dt: 0.016)
    }

    /// Advances NFSMW VLT vehicle physics for one frame and returns updated telemetry.
    @discardableResult
    func step(realDt rawDt: Float, input rawInput: RTDriveInput, probe: RTRoadProbeSample) -> RTDriveTelemetry {
        let realDt = max(0.0005, min(0.05, rawDt))
        var input = rawInput
        gamepad.mergeGamepad(into: &input)

        // Reset one-shot triggers each frame
        state.shiftPopTrigger = false
        state.bovTrigger = false
        state.impactIntensity = 0.0
        state.scrapeIntensity = 0.0

        // 1. Speedbreaker (0.35x slow-mo bullet-time + recharging 10s reservoir)
        let wantsSB = input.speedbreaker && state.speedbreakerCharge > 0.04 && abs(velForward) > 6.0
        if wantsSB {
            state.speedbreakerActive = true
            state.speedbreakerCharge = max(0.0, state.speedbreakerCharge - realDt * 0.115) // ~8.7s full burn
        } else {
            state.speedbreakerActive = false
            let rechargeRate: Float = abs(velForward) > 16.0 ? 0.055 : 0.025
            state.speedbreakerCharge = min(1.0, state.speedbreakerCharge + realDt * rechargeRate)
        }
        let targetSB: Float = state.speedbreakerActive ? 1.0 : 0.0
        state.speedbreakerBlend += (targetSB - state.speedbreakerBlend) * min(1.0, realDt * 11.0)
        state.effectiveTimeScale = 1.0 - 0.65 * state.speedbreakerBlend // 1.0 -> 0.35x
        let simDt = realDt * state.effectiveTimeScale

        // Substep if frame dt is large for rock-solid spring/drift stability
        let substeps = simDt > 0.020 ? 2 : 1
        let dt = simDt / Float(substeps)

        for _ in 0..<substeps {
            stepSub(dt: dt, input: input, probe: probe)
        }

        // Smooth visual effects outputs
        let flameTarget: Float = state.nosActive ? 1.0 : backfireDecay
        state.nosFlameIntensity += (flameTarget - state.nosFlameIntensity) * min(1.0, realDt * 18.0)
        backfireDecay = max(0.0, backfireDecay - realDt * 4.5)

        let fovTarget: Float = state.nosActive ? 9.5 : 0.0
        state.nosFovKickDeg += (fovTarget - state.nosFovKickDeg) * min(1.0, realDt * 6.5)

        // Turbo BOV flutter trigger on sharp throttle lift at boost
        if prevThrottle > 0.55 && input.throttle < 0.15 && state.turboBoostNorm > 0.40 {
            state.bovTrigger = true
            backfireDecay = max(backfireDecay, 0.55)
        }
        prevThrottle = input.throttle

        return state
    }

    private func stepSub(dt: Float, input: RTDriveInput, probe: RTRoadProbeSample) {
        let throttle = clamp01(input.throttle)
        let brake = clamp01(input.brake)
        let speedAbs = hypot(velForward, velLateral)
        let speedKmh = speedAbs * 3.6
        let speedFrac = clamp01(speedKmh / 280.0)

        // 2. NOS Management (Shift / Z / Gamepad A)
        let canUseNos = input.nos && throttle > 0.15 && state.nosCharge > 0.015 && state.gear >= 1
        state.nosActive = canUseNos
        if canUseNos {
            let drainPerSec = spec.nosFlowRate / max(1.0, spec.nosCapacity * 2.4)
            state.nosCharge = max(0.0, state.nosCharge - drainPerSec * dt)
        } else {
            // NFSMW authentic velocity + drift NOS auto-recharge
            let speedRecharge = speedKmh > 75.0 ? (1.0 / max(12.0, spec.nosRechargeMaxSec)) : 0.012
            let driftBonus = state.driftIntensity * 0.055
            state.nosCharge = min(1.0, state.nosCharge + (speedRecharge + driftBonus) * dt)
        }

        // 3. Turbo Spool
        if spec.turboPSI > 0.5 || spec.turboHighBoost > 0.01 {
            if throttle > 0.25 && !state.isShifting {
                let rpmLoad = clamp01((state.rpm - 2200.0) / max(1000.0, spec.redlineRPM - 2200.0))
                let targetBoost = clamp01(throttle * (0.35 + 0.65 * rpmLoad) + (state.nosActive ? 0.25 : 0.0))
                state.turboBoostNorm += (targetBoost - state.turboBoostNorm) * min(1.0, dt / max(0.15, spec.turboSpoolUp * 0.35))
            } else {
                state.turboBoostNorm = max(0.0, state.turboBoostNorm - dt / max(0.10, spec.turboSpoolDown))
            }
        } else {
            state.turboBoostNorm = 0.0
        }

        // 4. Reverse / Forward Gear + 6-Speed Automatic Transmission & VLT Torque
        if shiftTimer > 0.0 {
            shiftTimer = max(0.0, shiftTimer - dt)
            state.isShifting = shiftTimer > 0.0
        } else {
            state.isShifting = false
        }
        shiftCooldown = max(0.0, shiftCooldown - dt)

        // Engaging reverse when stopped and holding brake
        var reverseDrive = false
        if velForward <= 0.6 && brake > 0.2 && throttle < 0.1 {
            state.gear = -1
            reverseDrive = true
        } else if state.gear <= 0 && throttle > 0.05 {
            state.gear = 1
        } else if state.gear == -1 && throttle > 0.15 && velForward > -1.0 {
            state.gear = 1
        }

        let wheelRadius: Float = 0.33
        let gRatio: Float = (state.gear == -1) ? spec.reverseGearRatio : spec.forwardGearRatio(max(1, state.gear))
        let totalDriveRatio = gRatio * spec.finalGear

        // Engine RPM coupled to driven wheels with clutch slip at launch
        let wheelAngularVel = abs(velForward) / wheelRadius
        let coupledRPM = (wheelAngularVel * totalDriveRatio * 60.0) / (2.0 * .pi)
        let launchBlend = clamp01(abs(velForward) / 14.0)
        let targetRPM: Float
        if state.gear == 0 {
            targetRPM = spec.idleRPM + throttle * (spec.redlineRPM - spec.idleRPM) * 0.72
        } else {
            let clutchRPM = spec.idleRPM + throttle * 3200.0 + (state.nosActive ? 800.0 : 0.0)
            targetRPM = clutchRPM * (1.0 - launchBlend) + coupledRPM * launchBlend
        }

        // Smooth RPM response weighted by flywheel inertia
        let rpmRate: Float = state.isShifting ? 9.0 : (throttle > 0.1 ? 16.0 : 7.5)
        state.rpm += (max(spec.idleRPM, min(spec.maxRPM, targetRPM)) - state.rpm) * min(1.0, dt * rpmRate)
        if state.rpm > spec.redlineRPM + 220.0 {
            // Soft NFSMW rev-limiter bounce
            state.rpm = spec.redlineRPM - 90.0
        }

        // Auto-upshift / auto-downshift
        if state.gear >= 1 && shiftCooldown <= 0.0 && !state.isShifting {
            let upshiftRPM = spec.redlineRPM - max(120.0, spec.optimalShiftRPM * 0.32)
            let downshiftRPM = spec.idleRPM + (spec.redlineRPM - spec.idleRPM) * 0.42
            if state.rpm >= upshiftRPM && state.gear < spec.forwardGearCount && throttle > 0.15 {
                state.gear += 1
                shiftTimer = min(0.14, spec.shiftSpeed * 0.58)
                shiftCooldown = 0.28
                state.isShifting = true
                if state.rpmNorm > 0.78 {
                    state.shiftPopTrigger = true
                    backfireDecay = max(backfireDecay, 0.85)
                }
            } else if state.gear > 1 && coupledRPM < downshiftRPM && abs(velForward) > 2.0 {
                state.gear -= 1
                shiftCooldown = 0.24
            }
        }
        state.rpmNorm = clamp01((state.rpm - spec.idleRPM) / max(1000.0, spec.redlineRPM - spec.idleRPM))

        // Compute longitudinal driving / braking / aerodynamic forces
        let baseTorqueNm = spec.sampleTorqueNm(at: state.rpm)
        let turboMul: Float = 1.0 + state.turboBoostNorm * max(0.0, spec.turboHighBoost)
        let nosMul: Float = state.nosActive ? (1.0 + spec.nosTorqueBoost * 0.65) : 1.0
        let shiftCut: Float = state.isShifting ? 0.18 : 1.0

        var driveForce: Float = 0.0
        if reverseDrive {
            driveForce = -brake * baseTorqueNm * spec.reverseGearRatio * spec.finalGear * 0.45 / wheelRadius
        } else if throttle > 0.01 && state.gear >= 1 {
            // Wheel-force traction cap based on VLT tire grip (AWD gets stronger launch traction!)
            let tractionBonus: Float = 1.0 + spec.torqueSplit * 0.22
            let maxTractionForce = spec.massKg * 9.81 * 0.55 * spec.staticGripRear * spec.gripScaleRear * tractionBonus * (state.nosActive ? 1.28 : 1.0)
            let rawWheelForce = throttle * baseTorqueNm * totalDriveRatio * 0.86 * turboMul * nosMul * shiftCut / wheelRadius
            driveForce = min(rawWheelForce, maxTractionForce)
        }

        // Aerodynamic drag + rolling resistance + engine braking + foot/hand brakes
        let aeroDrag = 0.5 * 1.225 * spec.dragCoefficient * 2.18 * velForward * abs(velForward)
        let aeroDownforce = 0.5 * 1.225 * spec.aeroCoefficient * 2.2 * speedAbs * speedAbs
        let rollingResist: Float = (abs(velForward) > 0.05) ? (velForward > 0 ? 1.0 : -1.0) * (spec.massKg * 0.16) : 0.0
        let engBrakeForce: Float = (throttle < 0.05 && velForward > 1.0) ? (spec.engineBraking * 950.0 * (0.4 + 0.6 * state.rpmNorm)) : 0.0

        var brakeForce: Float = 0.0
        if !reverseDrive && brake > 0.01 && velForward > 0.1 {
            let brakeStrength = (spec.brakeFront + spec.brakeRear) * 19.5
            brakeForce = brake * brakeStrength
        }
        if input.handbrake && abs(velForward) > 0.5 {
            brakeForce += spec.ebrake * 5.2
        }

        let prevVelForward = velForward
        let netLongForce = driveForce - aeroDrag - rollingResist - engBrakeForce - (velForward >= 0 ? brakeForce : -brakeForce)
        let accelLong = netLongForce / spec.massKg
        velForward += accelLong * dt

        // Clean stop clamp (avoid brake sign flip oscillation)
        if !reverseDrive && brake > 0.05 && prevVelForward > 0.0 && velForward < 0.0 {
            velForward = 0.0
        }
        // Top speed clamp (generous ~345 km/h with NOS, ~310 km/h naturally by drag/gears; -65 km/h reverse)
        let maxFwdMs: Float = state.nosActive ? 98.0 : 88.0
        velForward = max(-18.0, min(maxFwdMs, velForward))

        // 5. Speed-Sensitive Steering + NFSMW Arcade-Sim Drift & Handbrake Slip Model
        let steerRespRate: Float = input.steer != 0 ? 9.5 : 8.0
        smoothSteerInput += (max(-1.0, min(1.0, input.steer)) - smoothSteerInput) * min(1.0, dt * steerRespRate)

        let sbSteerBoost: Float = 1.0 + 0.40 * state.speedbreakerBlend
        let maxSteerDeg = (28.0 - 15.5 * speedFrac) * spec.steering * sbSteerBoost
        state.steerDeg = max(-34.0, min(34.0, smoothSteerInput * maxSteerDeg))
        let steerRad = state.steerDeg * .pi / 180.0

        // Lateral tire grip & slip angle dynamics
        let downforceGrip = 1.0 + clamp01(aeroDownforce / (spec.massKg * 9.81)) * 0.25
        let baseFrontGrip = spec.staticGripFront * spec.gripScaleFront * downforceGrip
        var rearGrip = spec.staticGripRear * spec.gripScaleRear * downforceGrip

        // Handbrake or high-power RWD launch/cornering kicks tail into controlled NFSMW power-slide
        let rwdBias = 1.0 - spec.torqueSplit // 1.0 for RWD, 0.5 for Gallardo AWD
        let powerOversteer = (throttle > 0.72 && abs(smoothSteerInput) > 0.35 && speedKmh > 45.0)
            ? (0.20 * rwdBias * (state.nosActive ? 1.25 : 1.0)) : 0.0
        if input.handbrake {
            rearGrip *= 0.46
        } else {
            rearGrip *= (1.0 - powerOversteer)
        }
        // Speedbreaker snaps grip & yaw authority so player can thread roadblocks effortlessly!
        if state.speedbreakerBlend > 0.05 {
            rearGrip *= (1.0 + 0.45 * state.speedbreakerBlend)
        }

        let dirSign: Float = velForward >= -0.2 ? 1.0 : -1.0
        let bikeYawRate = (abs(velForward) > 0.3)
            ? dirSign * (abs(velForward) / max(2.0, spec.wheelBase)) * tan(steerRad) * (0.72 + 0.28 * clamp01(baseFrontGrip / 2.2))
            : 0.0
        let handbrakeYawKick: Float = (input.handbrake && abs(velForward) > 6.0)
            ? steerRad * 1.45 * clamp01(abs(velForward) / 22.0) : 0.0

        let targetYawRate = max(-2.4, min(2.4, bikeYawRate + handbrakeYawKick))
        let yawLerpRate = (5.5 + spec.yawSpeed * 12.0) * (1.0 + 0.6 * state.speedbreakerBlend)
        yawRate += (targetYawRate - yawRate) * min(1.0, dt * yawLerpRate)

        // Lateral velocity evolution (centrifugal force vs rear tire lateral restoring force)
        if abs(velForward) > 0.6 {
            let centrifugal = -velForward * yawRate * (input.handbrake ? 0.62 : (0.28 + 0.16 * rwdBias))
            let lateralDamp = rearGrip * (input.handbrake ? 2.1 : 5.4)
            velLateral += (centrifugal - velLateral * lateralDamp) * dt
            // Clamp slip angle cleanly to +-32 deg (authentic NFSMW arcade-drift ceiling)
            let maxLat = abs(velForward) * tan(32.0 * .pi / 180.0)
            velLateral = max(-maxLat, min(maxLat, velLateral))
        } else {
            velLateral *= max(0.0, 1.0 - dt * 10.0)
            yawRate *= max(0.0, 1.0 - dt * 10.0)
        }

        let slipAngle = (abs(velForward) > 1.5) ? atan2(velLateral, abs(velForward)) : 0.0
        state.slipAngleRad = slipAngle
        state.driftIntensity = clamp01((abs(slipAngle) - 0.09) / 0.34) * clamp01(speedKmh / 35.0)
            + (input.handbrake && speedKmh > 18.0 ? 0.45 : 0.0)
        state.driftIntensity = clamp01(state.driftIntensity)

        // Advance world heading & velocity
        if probe.hasWorld {
            state.headingRad += yawRate * dt
        } else {
            state.visualYawRad = steerRad * 0.20 * speedFrac + slipAngle * 0.45
        }

        let fwd = SIMD3<Float>(sin(state.headingRad), 0.0, cos(state.headingRad))
        let right = SIMD3<Float>(cos(state.headingRad), 0.0, -sin(state.headingRad))
        let velXZ = fwd * velForward + right * velLateral
        state.velocityWorld = SIMD3<Float>(velXZ.x, velVertical, velXZ.z)

        // 6. World Motion, Wall Sliding Collisions & 4-Wheel BVH Elevation + Airborne Gravity
        if probe.hasWorld {
            var step = velXZ * dt

            // Front wall impact & smooth barrier sliding
            if probe.wallDist.x < 2.35 && probe.wallNormalFront.w > 0.5 {
                let wn = simd_normalize(SIMD3<Float>(probe.wallNormalFront.x, 0, probe.wallNormalFront.z))
                let intoWall = simd_dot(step, wn)
                if intoWall < 0.0 {
                    let closingSpeed = -simd_dot(velXZ, wn)
                    if closingSpeed > 4.0 {
                        state.impactIntensity = max(state.impactIntensity, clamp01(closingSpeed / 32.0))
                    }
                    step -= wn * intoWall
                    velForward *= 0.82
                    velLateral *= 0.65
                }
            }
            // Side wall separation & scrape intensity
            if probe.wallNormalSide.w != 0.0 {
                let sideDist = (probe.wallNormalSide.w < 0) ? probe.wallDist.z : probe.wallDist.w
                if sideDist < 1.18 {
                    let sn = simd_normalize(SIMD3<Float>(probe.wallNormalSide.x, 0, probe.wallNormalSide.z))
                    state.worldPosition += sn * (1.18 - sideDist) * 0.55
                    if speedKmh > 15.0 {
                        state.scrapeIntensity = max(state.scrapeIntensity, clamp01(speedKmh / 140.0))
                    }
                    velForward *= (1.0 - min(0.12, dt * 1.2))
                }
            }

            state.worldPosition += step

            // 4-Wheel GPU BVH Elevation & Airborne Physics
            let wY = probe.wheelHitY
            let hits = [wY.x, wY.y, wY.z, wY.w].filter { $0 > -5000.0 }
            if !hits.isEmpty {
                let avgY = hits.reduce(0.0, +) / Float(hits.count)
                let heightAboveGround = state.worldPosition.y - avgY
                if heightAboveGround > 0.35 && velVertical > -1.0 {
                    // Airborne over crest/jump!
                    state.isAirborne = true
                    velVertical -= 9.81 * dt
                    state.worldPosition.y += velVertical * dt
                    if state.worldPosition.y <= avgY {
                        state.worldPosition.y = avgY
                        let landImpact = clamp01(abs(velVertical) / 10.0)
                        pitchVel += landImpact * 0.25
                        state.impactIntensity = max(state.impactIntensity, landImpact * 0.65)
                        velVertical = 0.0
                        state.isAirborne = false
                    }
                } else {
                    if wasAirborne && velVertical < -2.0 {
                        pitchVel += clamp01(abs(velVertical) / 9.0) * 0.22
                    }
                    state.isAirborne = false
                    velVertical = 0.0
                    state.worldPosition.y += (avgY - state.worldPosition.y) * min(1.0, dt * 20.0)
                }

                if wY.x > -5000.0 && wY.y > -5000.0 && wY.z > -5000.0 && wY.w > -5000.0 {
                    let frontY = 0.5 * (wY.x + wY.y)
                    let rearY = 0.5 * (wY.z + wY.w)
                    let leftY = 0.5 * (wY.x + wY.z)
                    let rightY = 0.5 * (wY.y + wY.w)
                    let targetTPitch = max(-0.24, min(0.24, -atan2(frontY - rearY, max(2.2, spec.wheelBase))))
                    let targetTRoll = max(-0.20, min(0.20, atan2(leftY - rightY, max(1.4, spec.trackWidthFront))))
                    state.terrainPitchRad += (targetTPitch - state.terrainPitchRad) * min(1.0, dt * 13.0)
                    state.terrainRollRad += (targetTRoll - state.terrainRollRad) * min(1.0, dt * 13.0)
                }
            } else {
                // No road hit underneath: gentle gravity without falling infinitely
                state.isAirborne = true
                velVertical = max(-18.0, velVertical - 9.81 * dt)
                state.worldPosition.y += velVertical * dt
            }
            wasAirborne = state.isAirborne
            state.visualYawRad = state.headingRad
        } else {
            state.roadScrollZ += velForward * dt
        }

        // 7. 2nd-Order Spring-Damper Suspension Squat / Dive / Body Roll
        let springScale = 600.0 / max(300.0, 0.5 * (spec.springStiffnessFront + spec.springStiffnessRear))
        let swayScale = 220.0 / max(100.0, 0.5 * (spec.swaybarStiffnessFront + spec.swaybarStiffnessRear))

        // Negative pitchRad = nose-up squat on launch/NOS; positive pitchRad = nose-down brake dive
        let targetPitch = max(-0.042, min(0.048, -accelLong * 0.0022 * springScale))
        let latAccel = velForward * yawRate
        let targetRoll = max(-0.075, min(0.075, -latAccel * 0.0034 * swayScale))

        let kPitch: Float = 85.0
        let cPitch: Float = 13.5
        pitchVel += ((targetPitch - state.pitchRad) * kPitch - pitchVel * cPitch) * dt
        state.pitchRad = max(-0.06, min(0.06, state.pitchRad + pitchVel * dt))

        let kRoll: Float = 92.0
        let cRoll: Float = 14.0
        rollVel += ((targetRoll - state.rollRad) * kRoll - rollVel * cRoll) * dt
        state.rollRad = max(-0.09, min(0.09, state.rollRad + rollVel * dt))

        // 8. Final Kinematics
        state.speedMs = hypot(velForward, velLateral)
        state.speedKmh = state.speedMs * 3.6
        state.wheelSpinRad += (velForward / wheelRadius) * dt
    }

    private func updateGearAndRPMFromSpeed(throttle: Float, dt: Float) {
        let speedMs = max(0.0, velForward)
        let wheelRadius: Float = 0.33
        var chosenGear = 1
        for g in 1...spec.forwardGearCount {
            let ratio = spec.forwardGearRatio(g) * spec.finalGear
            let rpm = (speedMs / wheelRadius) * ratio * 60.0 / (2.0 * .pi)
            chosenGear = g
            if rpm < spec.redlineRPM * 0.90 {
                break
            }
        }
        state.gear = speedMs < 0.3 ? 0 : chosenGear
        let ratio = spec.forwardGearRatio(max(1, chosenGear)) * spec.finalGear
        let rpm = max(spec.idleRPM, min(spec.redlineRPM, (speedMs / wheelRadius) * ratio * 60.0 / (2.0 * .pi)))
        state.rpm = rpm
        state.rpmNorm = clamp01((rpm - spec.idleRPM) / max(1000.0, spec.redlineRPM - spec.idleRPM))
        _ = throttle
        _ = dt
    }

    /// Computes NFSMW-authentic chase camera azimuth, distance & FOV with drift lag, NOS kick & Speedbreaker zoom.
    func chaseCameraTarget() -> (target: SIMD3<Float>, azimuthDeg: Float, distance: Float, fovDeg: Float) {
        let headingDeg = state.headingRad * 180.0 / .pi
        let slipDeg = state.slipAngleRad * 180.0 / .pi
        let speedFrac = clamp01(state.speedKmh / 245.0)
        // Camera smoothly lags behind velocity vector during drifts so side profile of the M3 GTR shines!
        let az = headingDeg + 180.0 + state.steerDeg * 0.34 - slipDeg * 0.55
        let dist = 4.55 + 0.38 * speedFrac + (state.nosActive ? 0.36 : 0.0) - 0.22 * state.speedbreakerBlend
        let fov = 44.0 + 17.0 * speedFrac + state.nosFovKickDeg - 3.5 * state.speedbreakerBlend
        let tgt = SIMD3<Float>(state.worldPosition.x, state.worldPosition.y + 0.72, state.worldPosition.z)
        return (tgt, az, dist, fov)
    }

    @inline(__always)
    private func clamp01(_ x: Float) -> Float {
        return max(0.0, min(1.0, x))
    }
}
