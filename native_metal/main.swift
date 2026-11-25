import AppKit
import Foundation
import Metal
import MetalKit

final class ShowcaseMTKView: MTKView {
    weak var rendererRef: PBRRenderer?
    var onScreenshotRequested: (() -> Void)?
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
    }

    override func scrollWheel(with event: NSEvent) {
        guard let r = rendererRef else { return }
        r.camera.distance = max(2.4, min(14.0, r.camera.distance - Float(event.scrollingDeltaY) * 0.12))
    }

    override func keyDown(with event: NSEvent) {
        guard let r = rendererRef, let chars = event.charactersIgnoringModifiers?.lowercased() else { return }
        switch chars {
        case "1":
            r.activePresetKey = "golden_hour"
        case "2":
            r.activePresetKey = "daylight"
        case "3":
            r.activePresetKey = "night_rain"
        case "l":
            r.liveryEnabled.toggle()
        case "r":
            r.camera.reset()
        case "p":
            onScreenshotRequested?()
        case "q", "\u{1b}":
            NSApp.terminate(nil)
        default:
            super.keyDown(with: event)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var mtkView: ShowcaseMTKView!
    private let renderer: PBRRenderer
    private let sceneURL: URL

    init(renderer: PBRRenderer, sceneURL: URL) {
        self.renderer = renderer
        self.sceneURL = sceneURL
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
        window.title = "NFSMW: Rockport 2026 — Native Metal PBR (\(renderer.device.name))"
        window.delegate = self

        mtkView = ShowcaseMTKView(frame: rect, device: renderer.device)
        mtkView.colorPixelFormat = .bgra8Unorm_srgb
        mtkView.preferredFramesPerSecond = 120
        mtkView.delegate = renderer
        mtkView.rendererRef = renderer
        mtkView.onScreenshotRequested = { [weak self] in
            guard let self else { return }
            let outURL = self.sceneURL.deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("interactive_capture_\(self.renderer.activePresetKey).png")
            do {
                try self.renderer.renderHeadless(width: 1920, height: 1080, outURL: outURL)
                print("Saved screenshot -> \(outURL.path)")
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

struct CLIOptions {
    var headless = false
    var scenePath = "build/metal_wave3/scene/scene.json"
    var outPath = "build/metal_wave3/render.png"
    var width = 1920
    var height = 1080
    var preset = "golden_hour"
    var livery = true
    var azimuth: Float? = nil
    var elevation: Float? = nil
    var distance: Float? = nil
}

func parseArgs() -> CLIOptions {
    var opts = CLIOptions()
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--headless":
            opts.headless = true
        case "--scene":
            if i + 1 < args.count { i += 1; opts.scenePath = args[i] }
        case "--out":
            if i + 1 < args.count { i += 1; opts.outPath = args[i] }
        case "--width":
            if i + 1 < args.count, let v = Int(args[i + 1]) { i += 1; opts.width = v }
        case "--height":
            if i + 1 < args.count, let v = Int(args[i + 1]) { i += 1; opts.height = v }
        case "--preset":
            if i + 1 < args.count { i += 1; opts.preset = args[i] }
        case "--no-livery":
            opts.livery = false
        case "--azimuth":
            if i + 1 < args.count, let v = Float(args[i + 1]) { i += 1; opts.azimuth = v }
        case "--elevation":
            if i + 1 < args.count, let v = Float(args[i + 1]) { i += 1; opts.elevation = v }
        case "--distance":
            if i + 1 < args.count, let v = Float(args[i + 1]) { i += 1; opts.distance = v }
        case "--help", "-h":
            print("Usage: RockportMetalPBR [--headless] --scene <scene.json> [--out <out.png>] [--width 1920] [--height 1080] [--preset golden_hour|daylight|night_rain] [--no-livery]")
            exit(0)
        default:
            break
        }
        i += 1
    }
    return opts
}

let opts = parseArgs()
guard let device = MTLCreateSystemDefaultDevice() else {
    fputs("Error: Metal GPU device not available.\n", stderr)
    exit(1)
}

let sceneURL = URL(fileURLWithPath: opts.scenePath)
do {
    let renderer = try PBRRenderer(
        device: device,
        sceneURL: sceneURL,
        presetKey: opts.preset,
        liveryEnabled: opts.livery
    )
    if let az = opts.azimuth { renderer.camera.azimuthDeg = az }
    if let el = opts.elevation { renderer.camera.elevationDeg = el }
    if let dist = opts.distance { renderer.camera.distance = dist }

    if opts.headless {
        let outURL = URL(fileURLWithPath: opts.outPath)
        try renderer.renderHeadless(width: opts.width, height: opts.height, outURL: outURL)
        print("Rendered \(opts.width)x\(opts.height) (\(opts.preset)) on \(device.name) -> \(outURL.path)")
    } else {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = AppDelegate(renderer: renderer, sceneURL: sceneURL)
        app.delegate = delegate
        app.run()
    }
} catch {
    fputs("RockportMetalPBR error: \(error)\n", stderr)
    exit(1)
}
