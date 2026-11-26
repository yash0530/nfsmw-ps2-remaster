import AppKit
import Foundation
import Metal
import MetalKit

final class RTShowcaseMTKView: MTKView {
    weak var rendererRef: RTRenderer?
    var onScreenshotRequested: (() -> Void)?
    var onCarChanged: ((RTCarProfile) -> Void)?
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
        r.resetAccumulation()
    }

    override func scrollWheel(with event: NSEvent) {
        guard let r = rendererRef else { return }
        r.camera.distance = max(2.4, min(14.0, r.camera.distance - Float(event.scrollingDeltaY) * 0.12))
        r.resetAccumulation()
    }

    override func keyDown(with event: NSEvent) {
        guard let r = rendererRef else { return }
        let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""
        switch event.keyCode {
        case 126: r.throttleHeld = true; return
        case 125: r.brakeHeld = true; return
        case 123: r.steerLeftHeld = true; return
        case 124: r.steerRightHeld = true; return
        default: break
        }
        switch chars {
        case "w": r.throttleHeld = true
        case "s": r.brakeHeld = true
        case "a": r.steerLeftHeld = true
        case "d": r.steerRightHeld = true
        case "c":
            r.chaseCamEnabled.toggle()
            if r.chaseCamEnabled {
                r.camera.azimuthDeg = 180.0
                r.camera.elevationDeg = 7.8
                r.camera.distance = 4.65
                r.camera.fovDeg = 46.0
            } else {
                r.camera.reset()
            }
            r.resetAccumulation()
        case "1":
            r.activePresetKey = "golden_hour"
        case "2":
            r.activePresetKey = "night_neon"
        case "3":
            r.activePresetKey = "daylight"
        case "t":
            r.rtEnabled.toggle()
        case "n":
            r.denoiserEnabled.toggle()
        case "l":
            r.liveryEnabled.toggle()
        case "h":
            r.hudEnabled.toggle()
        case "g":
            let nextCar = r.scene.carProfile.next()
            do {
                try r.switchCar(to: nextCar)
                onCarChanged?(nextCar)
            } catch {
                print("Failed to switch car to \(nextCar.rawValue): \(error)")
            }
        case "r":
            r.speedKmh = 0.0
            r.steerDeg = 0.0
            r.carYawRad = 0.0
            r.carRollRad = 0.0
            r.carPitchRad = 0.0
            r.chaseCamEnabled = false
            r.camera.reset()
            try? r.rebuildTLAS(commandBuffer: nil)
            r.resetAccumulation()
        case "p":
            onScreenshotRequested?()
        case "q", "\u{1b}":
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
        default: break
        }
        switch chars {
        case "w": r.throttleHeld = false
        case "s": r.brakeHeld = false
        case "a": r.steerLeftHeld = false
        case "d": r.steerRightHeld = false
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
        window.title = "NFSMW: Rockport 2026 — \(renderer.scene.carProfile.displayName) — Metal 4 RT (\(renderer.device.name))"
        window.delegate = self

        mtkView = RTShowcaseMTKView(frame: rect, device: renderer.device)
        mtkView.colorPixelFormat = .bgra8Unorm_srgb
        mtkView.framebufferOnly = false
        mtkView.preferredFramesPerSecond = 120
        mtkView.delegate = renderer
        mtkView.rendererRef = renderer
        mtkView.onCarChanged = { [weak self] profile in
            guard let self else { return }
            self.window.title = "NFSMW: Rockport 2026 — \(profile.displayName) — Metal 4 RT (\(self.renderer.device.name))"
        }
        mtkView.onScreenshotRequested = { [weak self] in
            guard let self else { return }
            let outURL = URL(fileURLWithPath: "build/metal_rt_wave5/interactive_rt_\(self.renderer.scene.carProfile.rawValue)_\(self.renderer.activePresetKey).png")
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
        NSApp.terminate(nil)
    }
}

struct RTCLIOptions {
    var headless = false
    var scenePath: String? = nil
    var carKey: String = "m3_gtr"
    var configPath: String? = nil
    var synthetic = false
    var driveDemo = false
    var hud: Bool? = nil
    var speedKmh: Float = 145.0
    var steerDeg: Float = 12.0
    var outPath = "build/metal_rt_wave5/rt_render.png"
    var compareOutPath: String? = nil
    var width = 1920
    var height = 1080
    var spp = 64
    var preset = "golden_hour"
    var livery = true
    var rtEnabled = true
    var denoiserEnabled = true
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
        case "--hud":
            opts.hud = true
        case "--no-hud":
            opts.hud = false
        case "--car":
            if let v = nextVal() { opts.carKey = v }
        case "--no-livery":
            opts.livery = false
        case "--no-rt":
            opts.rtEnabled = false
        case "--no-denoise":
            opts.denoiserEnabled = false
        case "--scene":
            if let v = nextVal() { opts.scenePath = v }
        case "--config":
            if let v = nextVal() { opts.configPath = v }
        case "--out":
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
            RockportMetalRT — NFSMW: Rockport 2026 Apple Silicon Metal 4 Hardware Ray Tracing Engine
            Usage:
              RockportMetalRT --headless [--car m3_gtr|carrera_gt|gallardo] [--scene <dir/json>] [--config <lookdev.json>] [--synthetic] [--preset golden_hour|night_neon|daylight] [--spp 64] [--width 1920] [--height 1080] [--drive-demo --speed-kmh 165 --steer-deg 10] [--hud] [--out <png>] [--compare-out <png>]
              RockportMetalRT [--car m3_gtr|carrera_gt|gallardo] [--scene <dir/json>] [--config <lookdev.json>] [--synthetic] [--preset golden_hour] [--hud]
            Interactive Controls:
              W/S: Throttle/Brake | A/D: Steer | C: Chase/Orbit Cam | Mouse Drag: Orbit | Scroll: Zoom
              G: Cycle Garage Car | H: Toggle Digital HUD | 1/2/3: Presets | T: Toggle RT | N: Toggle Denoiser | L: Toggle Livery | R: Reset | P: Save 64-SPP PNG | Q/Esc: Quit
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
    let scene = try RTSceneBuilder.buildScene(
        device: device,
        commandQueue: queue,
        scenePath: opts.scenePath,
        carKey: opts.carKey,
        forceSynthetic: opts.synthetic
    )
    let metalURL = URL(fileURLWithPath: "native_metal_rt/RayTracing.metal")
    let renderer = try RTRenderer(device: device, scene: scene, metalFileURL: metalURL)
    renderer.forceSyntheticMode = opts.synthetic
    renderer.activePresetKey = opts.preset
    renderer.liveryEnabled = opts.livery
    renderer.rtEnabled = opts.rtEnabled
    renderer.denoiserEnabled = opts.denoiserEnabled
    renderer.hudEnabled = opts.hud ?? false

    if let cfgPath = opts.configPath {
        let cfg = try RTSceneBuilder.loadLookdevConfig(from: cfgPath)
        renderer.applyLookdevConfig(cfg)
    }

    if opts.driveDemo {
        renderer.driveDemoMode = true
        renderer.configureDrivePose(speedKmh: opts.speedKmh, steerDeg: opts.steerDeg, useChaseCam: true)
    }

    if let az = opts.azimuth { renderer.camera.azimuthDeg = az }
    if let el = opts.elevation { renderer.camera.elevationDeg = el }
    if let dist = opts.distance { renderer.camera.distance = dist }
    if let fov = opts.fov { renderer.camera.fovDeg = fov }

    if opts.headless {
        let outURL = URL(fileURLWithPath: opts.outPath)
        let t0 = CFAbsoluteTimeGetCurrent()
        try renderer.renderHeadless(
            width: opts.width,
            height: opts.height,
            spp: opts.spp,
            splitCompare: false,
            outURL: outURL
        )
        let dt = CFAbsoluteTimeGetCurrent() - t0
        print(String(format: "Metal RT GPU=%@ | supportsRaytracing=true | car=%@ | source=%@ | blas=%d | tris=%d | denoise=%@ | hud=%@ | %dx%d @ %d spp -> %@ (%.2fs)",
                     device.name, scene.carProfile.rawValue, scene.sourceLabel, scene.meshes.count, scene.totalTriangles,
                     renderer.denoiserEnabled ? "atrous4" : "off",
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
                outURL: cmpURL
            )
            let dt2 = CFAbsoluteTimeGetCurrent() - t1
            print(String(format: "Metal RT Split Compare (Left: Raster Baseline / Right: M5 Pro Hardware RT + Denoiser) -> %@ (%.2fs)",
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
