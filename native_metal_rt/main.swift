import AppKit
import Foundation
import Metal
import MetalKit

final class RTShowcaseMTKView: MTKView {
    weak var rendererRef: RTRenderer?
    var onScreenshotRequested: (() -> Void)?
    var onCarChanged: ((String) -> Void)?
    private var lastDrag: NSPoint = .zero

    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        lastDrag = event.locationInWindow
    }

    override func mouseDragged(with event: NSEvent) {
        guard let r = rendererRef else { return }
        let pt = event.locationInWindow
        let dx = Float(pt.x - lastDrag.x)
        let dy = Float(pt.y - lastDrag.y)
        lastDrag = pt
        r.camera.azimuthDeg -= dx * 0.35
        r.camera.elevationDeg = max(2.0, min(65.0, r.camera.elevationDeg - dy * 0.25))
        if r.denoiseMode == .off { r.resetAccumulation() }
    }

    override func scrollWheel(with event: NSEvent) {
        guard let r = rendererRef else { return }
        r.camera.distance = max(2.4, min(40.0, r.camera.distance - Float(event.scrollingDeltaY) * 0.18))
        if r.denoiseMode == .off { r.resetAccumulation() }
    }

    override func flagsChanged(with event: NSEvent) {
        guard let r = rendererRef else {
            super.flagsChanged(with: event)
            return
        }
        r.nosShiftHeld = event.modifierFlags.contains(.shift)
        super.flagsChanged(with: event)
    }

    override func keyDown(with event: NSEvent) {
        guard let r = rendererRef else { return }
        let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch event.keyCode {
        case 126: r.throttleHeld = true; return
        case 125: r.brakeHeld = true; return
        case 123: r.steerLeftHeld = true; return
        case 124: r.steerRightHeld = true; return
        case 49:  r.handbrakeHeld = true; return     // Space: Handbrake / Drift
        case 48:  r.speedbreakerHeld = true; return  // Tab: Speedbreaker
        case 53:  NSApp.terminate(nil); return       // Esc: Quit
        default: break
        }
        switch chars {
        case "w": r.throttleHeld = true
        case "s": r.brakeHeld = true
        case "a": r.steerLeftHeld = true
        case "d": r.steerRightHeld = true
        case "z": r.nosKeyHeld = true
        case "x": r.speedbreakerHeld = true
        case "f":
            r.triggerInstantPursuit()
            print("Pursuit active — Heat x\(r.gameWorld.heatLevel) | Cops=\(r.gameWorld.activeCopCount) | Bounty=$\(Int(r.gameWorld.bounty))")
        case "c":
            r.applyCameraMode(r.cameraMode.next())
        case "1":
            r.timeCycleEnabled = false
            r.explicitTimeOfDay = nil
            r.activePresetKey = "golden_hour"
        case "2":
            r.timeCycleEnabled = false
            r.explicitTimeOfDay = nil
            r.activePresetKey = "night_neon"
        case "3":
            r.timeCycleEnabled = false
            r.explicitTimeOfDay = nil
            r.activePresetKey = "daylight"
        case "4":
            r.explicitTimeOfDay = nil
            r.timeCycleEnabled.toggle()
        case "b":
            r.rainIntensity = (r.rainIntensity > 0.2) ? 0.0 : 0.88
        case "v":
            if let lm = r.cycleLandmark() {
                print("Teleported to landmark: \(lm.name)")
                onCarChanged?(r.scene.carDisplayName)
            }
        case "k":
            r.frameGenEnabled.toggle()
        case "t":
            r.rtEnabled.toggle()
        case "n":
            r.denoiseMode = r.denoiseMode.next()
        case "l":
            r.liveryEnabled.toggle()
        case "h":
            r.hudEnabled.toggle()
        case "g":
            let delta = event.modifierFlags.contains(.shift) ? -1 : 1
            let nextEntry = RTCarCatalog.next(after: r.scene.canonicalCarName, delta: delta)
            do {
                let swapped = try r.switchCar(toKey: nextEntry.canonicalName)
                onCarChanged?(swapped.carDisplayName)
            } catch {
                print("Failed to switch car to \(nextEntry.canonicalName): \(error)")
            }
        case "r":
            r.resetPlayer()
        case "p":
            onScreenshotRequested?()
        case "[":
            if let info = r.prevMusicTrack() {
                print("EA TRAX [\(info.index + 1)/\(info.total)]: \(info.artist) — \(info.title)")
            }
        case "]":
            if let info = r.nextMusicTrack() {
                print("EA TRAX [\(info.index + 1)/\(info.total)]: \(info.artist) — \(info.title)")
            }
        case "u":
            let muted = r.toggleMusicMute()
            print("EA TRAX Music: \(muted ? "MUTED" : "ON")")
        case "\u{1b}":
            NSApp.terminate(nil)
        default:
            super.keyDown(with: event)
        }
    }

    override func keyUp(with event: NSEvent) {
        guard let r = rendererRef else { return }
        let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch event.keyCode {
        case 126: r.throttleHeld = false; return
        case 125: r.brakeHeld = false; return
        case 123: r.steerLeftHeld = false; return
        case 124: r.steerRightHeld = false; return
        case 49:  r.handbrakeHeld = false; return
        case 48:  r.speedbreakerHeld = false; return
        default: break
        }
        switch chars {
        case "w": r.throttleHeld = false
        case "s": r.brakeHeld = false
        case "a": r.steerLeftHeld = false
        case "d": r.steerRightHeld = false
        case "z": r.nosKeyHeld = false
        case "x": r.speedbreakerHeld = false
        default: super.keyUp(with: event)
        }
    }
}

final class RTAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var mtkView: RTShowcaseMTKView!
    private let renderer: RTRenderer

    init(renderer: RTRenderer) {
        self.renderer = renderer
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let rect = NSRect(x: 120, y: 120, width: 1280, height: 720)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        let worldTag = renderer.world != nil ? " [\(renderer.activeLandmarkName)]" : ""
        window.title = "NFSMW: Rockport 2026 — \(renderer.scene.carDisplayName)\(worldTag) — Metal RT (\(renderer.device.name))"
        window.delegate = self

        mtkView = RTShowcaseMTKView(frame: rect, device: renderer.device)
        mtkView.colorPixelFormat = .bgra8Unorm_srgb
        mtkView.framebufferOnly = false
        mtkView.preferredFramesPerSecond = 120
        mtkView.delegate = renderer
        mtkView.rendererRef = renderer
        mtkView.onCarChanged = { [weak self] displayName in
            guard let self else { return }
            let wTag = self.renderer.world != nil ? " [\(self.renderer.activeLandmarkName)]" : ""
            self.window.title = "NFSMW: Rockport 2026 — \(displayName)\(wTag) — Metal RT (\(self.renderer.device.name))"
        }
        mtkView.onScreenshotRequested = { [weak self] in
            guard let self else { return }
            let outURL = URL(fileURLWithPath: "build/metal_rt_v06/interactive_rt_\(self.renderer.scene.canonicalCarName)_\(self.renderer.activePresetKey).png")
            do {
                try self.renderer.renderHeadless(width: 1920, height: 1080, spp: 64, splitCompare: false, outURL: outURL)
                print("Saved 64-SPP RT PNG -> \(outURL.path)")
            } catch {
                print("Screenshot failed: \(error)")
            }
        }

        window.contentView = mtkView
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(mtkView)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        renderer.audioEngine.stop()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        renderer.audioEngine.stop()
    }
}

struct RTCLIOptions {
    var headless = false
    var scenePath: String? = nil
    var worldPath: String? = nil
    var carKey: String = "m3_gtr"
    var configPath: String? = nil
    var synthetic = false
    var driveDemo = false
    var driveBenchFrames: Int? = nil
    var hud: Bool? = nil
    var pursuit = false
    var pursuitHeat: Int = 3
    var nos = false
    var speedbreaker = false
    var noAi = false
    var noAudio = false
    var noMusic = false
    var musicTrack: Int? = nil
    var showTrax = false
    var spawnPos: SIMD3<Float>? = nil
    var spawnYawDeg: Float? = nil
    var speedKmh: Float = 145.0
    var steerDeg: Float = 12.0
    var outPath = "build/metal_rt_wave5/rt_render.png"
    var compareOutPath: String? = nil
    var width = 1920
    var height = 1080
    var spp = 64
    var preset = "golden_hour"
    var landmark: String? = nil
    var timeOfDayHours: Float? = nil
    var rainIntensity: Float? = nil
    var livery = true
    var rtEnabled = true
    var denoiseMode: RTDenoiseMode? = nil
    var denoiserEnabled = true
    var frameGen = false
    var cameraMode: RTCameraMode? = nil
    var azimuth: Float? = nil
    var elevation: Float? = nil
    var distance: Float? = nil
    var fov: Float? = nil
}

func parseRTArgs() -> RTCLIOptions {
    var opts = RTCLIOptions()
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        let arg = args[i]
        func nextVal() -> String? {
            if i + 1 < args.count {
                i += 1
                return args[i]
            }
            return nil
        }
        switch arg {
        case "--headless":
            opts.headless = true
        case "--synthetic":
            opts.synthetic = true
        case "--drive-demo":
            opts.driveDemo = true
        case "--drive-bench":
            if let v = nextVal(), let n = Int(v) {
                opts.driveBenchFrames = max(1, n)
                opts.headless = true
            }
        case "--hud":
            opts.hud = true
        case "--no-hud":
            opts.hud = false
        case "--pursuit":
            opts.pursuit = true
            if i + 1 < args.count, let h = Int(args[i + 1]), (1...5).contains(h) {
                opts.pursuitHeat = h
                i += 1
            }
        case "--heat":
            if let v = nextVal(), let h = Int(v) {
                opts.pursuit = true
                opts.pursuitHeat = max(1, min(5, h))
            }
        case "--nos":
            opts.nos = true
        case "--speedbreaker":
            opts.speedbreaker = true
        case "--no-ai":
            opts.noAi = true
        case "--no-audio":
            opts.noAudio = true
        case "--no-music":
            opts.noMusic = true
        case "--music-track":
            if let v = nextVal(), let idx = Int(v) { opts.musicTrack = idx }
        case "--show-trax":
            opts.showTrax = true
        case "--spawn-pos":
            if let v = nextVal() {
                let parts = v.split(separator: ",").compactMap { Float($0.trimmingCharacters(in: .whitespaces)) }
                if parts.count == 3 {
                    opts.spawnPos = SIMD3<Float>(parts[0], parts[1], parts[2])
                }
            }
        case "--spawn-yaw":
            if let v = nextVal(), let f = Float(v) { opts.spawnYawDeg = f }
        case "--car":
            if let v = nextVal() { opts.carKey = v }
        case "--world":
            if let v = nextVal() { opts.worldPath = v }
        case "--denoise":
            if let v = nextVal() {
                let m = RTDenoiseMode.parse(v)
                opts.denoiseMode = m
                opts.denoiserEnabled = (m != .off)
            }
        case "--frame-gen":
            if let v = nextVal() {
                let low = v.lowercased()
                opts.frameGen = (low == "on" || low == "true" || low == "1" || low == "yes")
            } else {
                opts.frameGen = true
            }
        case "--camera-mode":
            if let v = nextVal() { opts.cameraMode = RTCameraMode.parse(v) }
        case "--no-livery":
            opts.livery = false
        case "--no-rt":
            opts.rtEnabled = false
        case "--no-denoise":
            opts.denoiserEnabled = false
            opts.denoiseMode = .off
        case "--scene":
            if let v = nextVal() { opts.scenePath = v }
        case "--config":
            if let v = nextVal() { opts.configPath = v }
        case "--out", "--output":
            if let v = nextVal() { opts.outPath = v }
        case "--compare-out":
            if let v = nextVal() { opts.compareOutPath = v }
        case "--width":
            if let v = nextVal(), let n = Int(v) { opts.width = max(64, n) }
        case "--height":
            if let v = nextVal(), let n = Int(v) { opts.height = max(64, n) }
        case "--spp":
            if let v = nextVal(), let n = Int(v) { opts.spp = max(1, n) }
        case "--preset":
            if let v = nextVal() { opts.preset = v }
        case "--landmark":
            if let v = nextVal() { opts.landmark = v }
        case "--time-of-day":
            if let v = nextVal(), let f = Float(v) { opts.timeOfDayHours = f }
        case "--rain":
            if let v = nextVal(), let f = Float(v) { opts.rainIntensity = max(0.0, min(1.0, f)) }
        case "--speed-kmh":
            if let v = nextVal(), let f = Float(v) { opts.speedKmh = f }
        case "--steer-deg":
            if let v = nextVal(), let f = Float(v) { opts.steerDeg = f }
        case "--azimuth":
            if let v = nextVal(), let f = Float(v) { opts.azimuth = f }
        case "--elevation":
            if let v = nextVal(), let f = Float(v) { opts.elevation = f }
        case "--distance":
            if let v = nextVal(), let f = Float(v) { opts.distance = f }
        case "--fov":
            if let v = nextVal(), let f = Float(v) { opts.fov = f }
        case "--help", "-h":
            print("""
            RockportMetalRT — NFSMW: Rockport 2026 Apple Silicon Hardware Ray Tracing + MetalFX Engine
            Usage:
              RockportMetalRT --headless [--world <city.rkpt>] [--car m3_gtr|carrera_gt|gallardo|...] [--scene <dir/json>] [--config <lookdev.json>] [--synthetic] [--preset golden_hour|night_neon|daylight] [--landmark <name|idx>] [--spawn-pos x,y,z] [--spawn-yaw <deg>] [--time-of-day <0..24>] [--rain <0..1>] [--denoise metalfx|svgf|off] [--frame-gen on|off] [--camera-mode chase|hero|street|overhead] [--spp 64] [--width 1920] [--height 1080] [--drive-demo --speed-kmh 165 --steer-deg 10] [--drive-bench 60] [--pursuit [1..5]] [--nos] [--speedbreaker] [--no-ai] [--no-audio] [--no-music] [--music-track <idx>] [--show-trax] [--hud|--no-hud] [--out|--output <png>] [--compare-out <png>]
              RockportMetalRT [--world <city.rkpt>] [--car m3_gtr|...] [--preset golden_hour] [--denoise metalfx|svgf|off] [--frame-gen on|off] [--camera-mode chase|hero|street|overhead] [--pursuit] [--hud] [--no-ai] [--no-audio] [--no-music] [--music-track <idx>] [--show-trax]
            Interactive Controls (Canonical Keymap):
              WASD / Arrows: Drive | Space: Handbrake/Drift | Shift / Z: NOS Boost | X / Tab: Speedbreaker
              F: Instant Pursuit (Heat 1->5) | V: Cycle Landmark | G / Shift+G: Cycle Garage Car | C: Cycle Camera
              [ / ]: Prev / Next EA TRAX Song | U: Mute/Unmute EA TRAX Music
              1/2/3/4: Presets & Live 24h | B: Rain | K: Frame-Gen | H: HUD | L: Livery | T: RT | N: Denoiser
              R: Reset Car & Pursuit | P: Save 64-SPP PNG | Mouse Drag: Orbit | Scroll: Zoom | Esc: Quit
            """)
            exit(0)
        default:
            break
        }
        i += 1
    }
    return opts
}

let opts = parseRTArgs()
guard let device = MTLCreateSystemDefaultDevice() else {
    fputs("Error: Metal device unavailable\n", stderr)
    exit(1)
}
guard device.supportsRaytracing else {
    fputs("Error: Metal hardware ray tracing is not supported on \(device.name)\n", stderr)
    exit(2)
}
guard let queue = device.makeCommandQueue() else {
    fputs("Error: Failed to create MTLCommandQueue\n", stderr)
    exit(1)
}

do {
    var worldPkg: RTWorldPackage? = nil
    if let wPath = opts.worldPath {
        let wURL = URL(fileURLWithPath: wPath)
        let tW = CFAbsoluteTimeGetCurrent()
        let pkg = try RTWorldLoader.load(url: wURL, device: device, commandQueue: queue)
        let dtW = (CFAbsoluteTimeGetCurrent() - tW) * 1000.0
        print(String(format: "Loaded RKPT world %@ | sections=%d | meshes=%d | instances=%d | materials=%d | textures=%d | worldTris=%d (%.1f ms)",
                     wURL.lastPathComponent, pkg.sections.count, pkg.meshes.count, pkg.instances.count,
                     pkg.materials.count, pkg.textures.count, pkg.totalTriangles, dtW))
        worldPkg = pkg
    }

    let scene = try RTSceneBuilder.buildScene(
        device: device,
        commandQueue: queue,
        scenePath: opts.scenePath,
        carKey: opts.carKey,
        forceSynthetic: opts.synthetic,
        skipProceduralWorld: (worldPkg != nil)
    )
    let metalURL = URL(fileURLWithPath: "native_metal_rt/RayTracing.metal")
    let audioHeadless = opts.headless || opts.noAudio
    let renderer = try RTRenderer(
        device: device,
        commandQueue: queue,
        scene: scene,
        world: worldPkg,
        metalFileURL: metalURL,
        headless: audioHeadless,
        aiEnabled: !opts.noAi
    )
    renderer.forceSyntheticMode = opts.synthetic
    renderer.benchmarkNosForced = opts.nos
    renderer.benchmarkSpeedbreakerForced = opts.speedbreaker
    if opts.pursuit {
        renderer.benchmarkPursuitHeat = opts.pursuitHeat
    }
    renderer.activePresetKey = opts.preset
    if let tod = opts.timeOfDayHours {
        renderer.explicitTimeOfDay = tod
        renderer.timeOfDayHours = tod
    }
    if let rInt = opts.rainIntensity {
        renderer.rainIntensity = rInt
    }
    renderer.liveryEnabled = opts.livery
    renderer.rtEnabled = opts.rtEnabled
    if let dm = opts.denoiseMode {
        renderer.denoiseMode = dm
    } else {
        renderer.denoiseMode = .metalfx
        renderer.denoiserEnabled = opts.denoiserEnabled
    }
    renderer.frameGenEnabled = opts.frameGen
    renderer.hudEnabled = opts.hud ?? false
    if opts.noMusic {
        renderer.disableMusicPlayback()
    }
    if let mt = opts.musicTrack {
        _ = renderer.selectMusicTrack(index: mt)
    }
    if opts.showTrax {
        renderer.forceShowMusicBanner = true
        renderer.syncMusicBannerFromAudio(triggerSlide: true)
    }

    if let lmQuery = opts.landmark {
        if let lm = renderer.teleportToLandmark(lmQuery) {
            print(String(format: "Landmark teleport -> %@ (%.1f, %.1f, %.1f) yaw=%.1f deg | activeWorldInsts=%d | skyOcc=%.2f",
                         lm.name, lm.spawnPos.x, lm.spawnPos.y, lm.spawnPos.z,
                         lm.spawnYawRad * 180.0 / .pi, renderer.activeWorldInstanceCount,
                         renderer.smoothedSkyOcclusion))
        } else {
            fputs("Warning: landmark '\(lmQuery)' not found in loaded world\n", stderr)
        }
    }
    if let sp = opts.spawnPos {
        let yawRad = (opts.spawnYawDeg ?? (renderer.carHeadingRad * 180.0 / .pi)) * .pi / 180.0
        renderer.teleportToPosition(sp, yawRad: yawRad, label: "Custom")
        print(String(format: "Custom spawn -> (%.1f, %.1f, %.1f) yaw=%.1f deg | activeWorldInsts=%d | skyOcc=%.2f (ceil=%.1fm)",
                     renderer.carWorldPosition.x, renderer.carWorldPosition.y, renderer.carWorldPosition.z,
                     yawRad * 180.0 / .pi, renderer.activeWorldInstanceCount,
                     renderer.smoothedSkyOcclusion, renderer.skyCeilingDist))
    }

    if let cfgPath = opts.configPath {
        let cfg = try RTSceneBuilder.loadLookdevConfig(from: cfgPath)
        renderer.applyLookdevConfig(cfg)
    }

    if opts.driveDemo {
        renderer.driveDemoMode = true
        renderer.configureDrivePose(speedKmh: opts.speedKmh, steerDeg: opts.steerDeg, useChaseCam: true)
    }

    if opts.pursuit && opts.driveBenchFrames == nil {
        renderer.triggerInstantPursuit(setHeat: opts.pursuitHeat, showcaseFraming: opts.headless)
    }
    if opts.headless && opts.driveBenchFrames == nil && (opts.nos || opts.speedbreaker) {
        renderer.throttleHeld = true
        if renderer.speedKmh < 80.0 {
            renderer.configureDrivePose(speedKmh: max(opts.speedKmh, 140.0), steerDeg: 0.0, useChaseCam: (opts.cameraMode == .chase || opts.driveDemo))
        }
        for _ in 0..<24 {
            _ = renderer.stepDrivePhysics(dt: 1.0 / 60.0)
        }
        renderer.throttleHeld = false
        try? renderer.rebuildTLAS(commandBuffer: nil)
    }

    if let cm = opts.cameraMode {
        renderer.applyCameraMode(cm)
    }

    if let az = opts.azimuth { renderer.camera.azimuthDeg = az }
    if let el = opts.elevation { renderer.camera.elevationDeg = el }
    if let dist = opts.distance { renderer.camera.distance = dist }
    if let fov = opts.fov { renderer.camera.fovDeg = fov }

    let totalTris = scene.totalTriangles + (worldPkg?.totalTriangles ?? 0) + (renderer.aiVehicles?.totalTriangles ?? 0)
    let totalBlas = scene.meshes.count + (worldPkg?.blasList.count ?? 0) + (renderer.aiVehicles?.blasList.count ?? 0)

    if let benchFrames = opts.driveBenchFrames {
        let outURL = URL(fileURLWithPath: opts.outPath)
        if opts.hud == nil { renderer.hudEnabled = true }
        let (avgMs, fps) = try renderer.runDriveBenchmark(frames: benchFrames, width: opts.width, height: opts.height, outURL: outURL)
        let worldBytes = renderer.worldStreamer?.estimatedWorldVRAMBytes ?? worldPkg?.estimatedVRAMBytes ?? 0
        let aiBytes = renderer.aiVehicles?.estimatedVRAMBytes ?? 0
        let vramMB = Double(worldBytes + aiBytes) / (1024.0 * 1024.0)
        print(String(format: "Metal RT DriveBench GPU=%@ | msl=%@ | supportsRaytracing=true | car=%@ | world=%@ | landmark=%@ | blas=%d | activeInsts=%d | aiInsts=%d | aiTraffic=%d | aiCops=%d | heat=%d | speed=%.1fkmh | skyOcc=%.2f | vram=%.1fMB | tris=%d | denoise=%@ | frameGen=%@ | frames=%d | avg=%.2f ms/frame (%.1f FPS) -> %@",
                     device.name, renderer.compiledMSLVersion, scene.carAlias,
                     worldPkg?.url.lastPathComponent ?? "procedural",
                     renderer.activeLandmarkName,
                     totalBlas, renderer.activeWorldInstanceCount,
                     renderer.activeAIInstanceCount,
                     renderer.gameWorld.activeTrafficCount, renderer.gameWorld.activeCopCount, renderer.gameWorld.heatLevel,
                     renderer.speedKmh, renderer.smoothedSkyOcclusion, vramMB, totalTris, renderer.effectiveDenoiseLabel,
                     renderer.effectiveFrameGenActive ? "on" : "off",
                     benchFrames, avgMs, fps, outURL.path))
    } else if opts.headless {
        let outURL = URL(fileURLWithPath: opts.outPath)
        let forceTemporal = (opts.denoiseMode == .metalfx)
        let denoiseTag: String
        if !renderer.denoiserEnabled {
            denoiseTag = "off"
        } else if forceTemporal {
            denoiseTag = renderer.effectiveDenoiseLabel
        } else {
            denoiseTag = "atrous4"
        }
        let t0 = CFAbsoluteTimeGetCurrent()
        try renderer.renderHeadless(
            width: opts.width,
            height: opts.height,
            spp: opts.spp,
            splitCompare: false,
            outURL: outURL,
            forceTemporalDenoise: forceTemporal
        )
        let dt = CFAbsoluteTimeGetCurrent() - t0
        print(String(format: "Metal RT GPU=%@ | msl=%@ | supportsRaytracing=true | car=%@ | source=%@ | world=%@ | landmark=%@ | blas=%d | tris=%d | denoise=%@ | hud=%@ | %dx%d @ %d spp -> %@ (%.2fs)",
                     device.name, renderer.compiledMSLVersion, scene.carAlias, scene.sourceLabel,
                     worldPkg?.url.lastPathComponent ?? "procedural",
                     renderer.activeLandmarkName,
                     totalBlas, totalTris, denoiseTag,
                     renderer.hudEnabled ? "on" : "off",
                     opts.width, opts.height, opts.spp, outURL.path, dt))

        if let cmpPath = opts.compareOutPath {
            let cmpURL = URL(fileURLWithPath: cmpPath)
            let t1 = CFAbsoluteTimeGetCurrent()
            try renderer.renderHeadless(
                width: opts.width,
                height: opts.height,
                spp: opts.spp,
                splitCompare: true,
                outURL: cmpURL,
                forceTemporalDenoise: false
            )
            let dt2 = CFAbsoluteTimeGetCurrent() - t1
            print(String(format: "Metal RT Split Compare (Left: Raster Baseline / Right: Apple Silicon Hardware RT + Denoiser) -> %@ (%.2fs)",
                         cmpURL.path, dt2))
        }
    } else {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = RTAppDelegate(renderer: renderer)
        app.delegate = delegate
        app.run()
    }
} catch {
    fputs("Error: \(error)\n", stderr)
    exit(1)
}
