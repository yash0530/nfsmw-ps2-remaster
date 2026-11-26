// macos_video_tools.swift — Native macOS AVFoundation + CoreGraphics video QA utility.
//
// Compiles cleanly with:
//   swiftc -O tools/macos_video_tools.swift -o build/flywheel/video_tools
//
// Subcommands:
//   probe <video_path> [--json]
//   extract --input <video> --out-dir <dir> [--interval <sec>] [--max-frames <n>] [--max-width <px>] [--json]
//   transcode --input <in> --output <out> [--preset <1280x720|1920x1080|high|medium|passthrough>] [--json]
//   find-window [--pid <pid>] [--owner <name>] [--json]
//   synth-video --output <out.mp4> [--duration <sec>] [--fps <fps>] [--width <w>] [--height <h>]
//               [--black-head <sec>] [--static-tail <sec>] [--clip-at <sec>] [--amber-at <sec>] [--json]

import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct ToolError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) {
        self.description = message
    }
}

private func emitJSON(_ obj: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
}

private func fourCCToString(_ code: FourCharCode) -> String {
    let bytes: [UInt8] = [
        UInt8((code >> 24) & 0xFF),
        UInt8((code >> 16) & 0xFF),
        UInt8((code >> 8) & 0xFF),
        UInt8(code & 0xFF),
    ]
    return String(bytes: bytes, encoding: .ascii)?.trimmingCharacters(in: .controlCharacters) ?? "unknown"
}

private func writePNGAtomically(_ image: CGImage, to dstURL: URL) throws -> Int {
    let fm = FileManager.default
    let parent = dstURL.deletingLastPathComponent()
    try fm.createDirectory(at: parent, withIntermediateDirectories: true)
    let tmpURL = parent.appendingPathComponent(".\(dstURL.lastPathComponent).tmp.\(UUID().uuidString)")
    defer {
        if fm.fileExists(atPath: tmpURL.path) {
            try? fm.removeItem(at: tmpURL)
        }
    }
    guard let dest = CGImageDestinationCreateWithURL(tmpURL as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        throw ToolError("Failed to create PNG image destination at \(tmpURL.path)")
    }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else {
        throw ToolError("Failed to finalize PNG write to \(tmpURL.path)")
    }
    let attrs = try fm.attributesOfItem(atPath: tmpURL.path)
    let fileSize = (attrs[.size] as? NSNumber)?.intValue ?? 0
    if fileSize <= 0 {
        throw ToolError("Wrote empty PNG file at \(tmpURL.path)")
    }
    if fm.fileExists(atPath: dstURL.path) {
        try fm.removeItem(at: dstURL)
    }
    try fm.moveItem(at: tmpURL, to: dstURL)
    return fileSize
}

private func printUsage() {
    let text = """
    Usage: video_tools <subcommand> [options]

    Subcommands:
      probe <video_path> [--json]
      extract --input <video> --out-dir <dir> [--interval <sec>] [--max-frames <n>] [--max-width <px>] [--json]
      transcode --input <in> --output <out> [--preset <1280x720|1920x1080|high|medium|passthrough>] [--json]
      find-window [--pid <pid>] [--owner <name>] [--json]
      synth-video --output <out.mp4> [--duration <sec>] [--fps <fps>] [--width <w>] [--height <h>]
                  [--black-head <sec>] [--static-tail <sec>] [--clip-at <sec>] [--amber-at <sec>] [--json]
    """
    print(text)
}

private func cmdProbe(_ args: [String]) -> Int32 {
    var inputPath: String?
    var jsonOutput = false
    var i = 0
    while i < args.count {
        let a = args[i]
        if a == "--json" {
            jsonOutput = true
            i += 1
        } else if a == "--input", i + 1 < args.count {
            inputPath = args[i + 1]
            i += 2
        } else if !a.hasPrefix("-") && inputPath == nil {
            inputPath = a
            i += 1
        } else {
            fputs("Error: unknown probe argument '\(a)'\n", stderr)
            return 2
        }
    }
    guard let path = inputPath else {
        fputs("Error: probe requires <video_path>\n", stderr)
        return 2
    }

    let url = URL(fileURLWithPath: path)
    let fm = FileManager.default
    let exists = fm.fileExists(atPath: url.path)
    var fileSize = 0
    if exists, let attrs = try? fm.attributesOfItem(atPath: url.path) {
        fileSize = (attrs[.size] as? NSNumber)?.intValue ?? 0
    }

    if !exists || fileSize == 0 {
        let payload: [String: Any] = [
            "path": url.path,
            "exists": exists,
            "file_size_bytes": fileSize,
            "valid": false,
            "readable": false,
            "error": exists ? "Video file is empty" : "Video file does not exist",
        ]
        if jsonOutput { emitJSON(payload) } else { fputs("Invalid video: \(payload["error"] ?? "")\n", stderr) }
        return 1
    }

    let asset = AVURLAsset(url: url, options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
    let readable = asset.isReadable
    let durationSec = CMTimeGetSeconds(asset.duration)
    let safeDuration = durationSec.isFinite && durationSec >= 0 ? durationSec : 0.0
    let videoTracks = asset.tracks(withMediaType: .video)
    let audioTracks = asset.tracks(withMediaType: .audio)

    var width = 0
    var height = 0
    var nominalFPS = 0.0
    var videoCodec = ""
    if let vt = videoTracks.first {
        let natural = vt.naturalSize.applying(vt.preferredTransform)
        width = Int(round(abs(natural.width)))
        height = Int(round(abs(natural.height)))
        nominalFPS = Double(vt.nominalFrameRate)
        if let desc = vt.formatDescriptions.first {
            let subType = CMFormatDescriptionGetMediaSubType(desc as! CMFormatDescription)
            videoCodec = fourCCToString(subType)
        }
    }

    var canDecodeFrame = false
    var decodeError: String?
    if readable && !videoTracks.isEmpty && safeDuration > 0 {
        let gen = AVAssetImageGenerator(asset: asset)
        gen.appliesPreferredTrackTransform = true
        gen.requestedTimeToleranceBefore = CMTime(seconds: 0.5, preferredTimescale: 600)
        gen.requestedTimeToleranceAfter = CMTime(seconds: 0.5, preferredTimescale: 600)
        let probeTime = CMTime(seconds: min(0.25, safeDuration * 0.5), preferredTimescale: 600)
        do {
            let cg = try gen.copyCGImage(at: probeTime, actualTime: nil)
            if cg.width > 0 && cg.height > 0 {
                canDecodeFrame = true
                if width == 0 { width = cg.width }
                if height == 0 { height = cg.height }
            }
        } catch {
            decodeError = "\(error.localizedDescription)"
        }
    } else if !readable {
        decodeError = "AVURLAsset is not readable (missing moov atom or unsupported container)"
    } else if videoTracks.isEmpty {
        decodeError = "No video track found"
    } else if safeDuration <= 0 {
        decodeError = "Video duration is zero"
    }

    let valid = readable && !videoTracks.isEmpty && safeDuration > 0 && canDecodeFrame
    let estFrames = nominalFPS > 0 ? Int(round(safeDuration * nominalFPS)) : 0

    var payload: [String: Any] = [
        "path": url.path,
        "exists": true,
        "file_size_bytes": fileSize,
        "valid": valid,
        "readable": readable,
        "can_decode_frame": canDecodeFrame,
        "duration_sec": (safeDuration * 1000.0).rounded() / 1000.0,
        "width": width,
        "height": height,
        "nominal_fps": (nominalFPS * 100.0).rounded() / 100.0,
        "estimated_frame_count": estFrames,
        "has_video": !videoTracks.isEmpty,
        "has_audio": !audioTracks.isEmpty,
        "video_codec": videoCodec,
    ]
    if let err = decodeError {
        payload["error"] = err
    }

    if jsonOutput {
        emitJSON(payload)
    } else {
        print("valid=\(valid) \(width)x\(height) dur=\(String(format: "%.2f", safeDuration))s fps=\(String(format: "%.1f", nominalFPS)) codec=\(videoCodec) audio=\(!audioTracks.isEmpty)")
    }
    return valid ? 0 : 1
}

private func cmdExtract(_ args: [String]) -> Int32 {
    var inputPath: String?
    var outDirPath: String?
    var intervalSec = 1.5
    var maxFrames = 600
    var maxWidth = 0
    var jsonOutput = false

    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "--input":
            guard i + 1 < args.count else { return 2 }
            inputPath = args[i + 1]; i += 2
        case "--out-dir":
            guard i + 1 < args.count else { return 2 }
            outDirPath = args[i + 1]; i += 2
        case "--interval":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v > 0 else { return 2 }
            intervalSec = v; i += 2
        case "--max-frames":
            guard i + 1 < args.count, let v = Int(args[i + 1]), v > 0 else { return 2 }
            maxFrames = v; i += 2
        case "--max-width":
            guard i + 1 < args.count, let v = Int(args[i + 1]), v >= 0 else { return 2 }
            maxWidth = v; i += 2
        case "--json":
            jsonOutput = true; i += 1
        default:
            fputs("Error: unknown extract argument '\(a)'\n", stderr)
            return 2
        }
    }

    guard let inPath = inputPath, let outDir = outDirPath else {
        fputs("Error: extract requires --input <video> --out-dir <dir>\n", stderr)
        return 2
    }

    let inURL = URL(fileURLWithPath: inPath)
    let outURL = URL(fileURLWithPath: outDir)
    do {
        try FileManager.default.createDirectory(at: outURL, withIntermediateDirectories: true)
    } catch {
        fputs("Error: failed to create out-dir \(outDir): \(error)\n", stderr)
        return 1
    }

    let asset = AVURLAsset(url: inURL, options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
    let durationSec = CMTimeGetSeconds(asset.duration)
    guard asset.isReadable, durationSec.isFinite, durationSec > 0 else {
        let payload: [String: Any] = [
            "ok": false,
            "input": inURL.path,
            "error": "Video asset is unreadable or has zero duration",
        ]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: unreadable video \(inPath)\n", stderr) }
        return 1
    }

    let videoTracks = asset.tracks(withMediaType: .video)
    guard let vt = videoTracks.first else {
        let payload: [String: Any] = ["ok": false, "input": inURL.path, "error": "No video track"]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: no video track\n", stderr) }
        return 1
    }

    let natural = vt.naturalSize.applying(vt.preferredTransform)
    let srcW = Int(round(abs(natural.width)))
    let srcH = Int(round(abs(natural.height)))

    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.requestedTimeToleranceBefore = CMTime(seconds: 0.05, preferredTimescale: 600)
    generator.requestedTimeToleranceAfter = CMTime(seconds: 0.05, preferredTimescale: 600)
    if maxWidth > 0 && srcW > maxWidth && srcH > 0 {
        let scaledH = Int(round(Double(srcH) * Double(maxWidth) / Double(srcW)))
        generator.maximumSize = CGSize(width: maxWidth, height: max(1, scaledH))
    }

    // Build sample timestamps from t=0.0 through end of video.
    var timestamps: [Double] = []
    var t = 0.0
    while t < durationSec - 0.05 && timestamps.count < maxFrames {
        timestamps.append(t)
        t += intervalSec
    }
    let tailTime = max(0.0, durationSec - 0.08)
    if timestamps.isEmpty || (tailTime - (timestamps.last ?? 0.0) >= min(0.35, intervalSec * 0.35) && timestamps.count < maxFrames) {
        timestamps.append(tailTime)
    }

    var extracted: [[String: Any]] = []
    var failures: [[String: Any]] = []

    for (idx, reqSec) in timestamps.enumerated() {
        let cmTime = CMTime(seconds: reqSec, preferredTimescale: 600)
        var actualCM = CMTime.zero
        var cgImage: CGImage?
        var errMsg: String?

        do {
            cgImage = try generator.copyCGImage(at: cmTime, actualTime: &actualCM)
        } catch {
            // Retry with looser tolerance near keyframes / end of stream
            let looseGen = AVAssetImageGenerator(asset: asset)
            looseGen.appliesPreferredTrackTransform = true
            looseGen.requestedTimeToleranceBefore = CMTime(seconds: 0.5, preferredTimescale: 600)
            looseGen.requestedTimeToleranceAfter = CMTime(seconds: 0.5, preferredTimescale: 600)
            if maxWidth > 0 && srcW > maxWidth && srcH > 0 {
                let scaledH = Int(round(Double(srcH) * Double(maxWidth) / Double(srcW)))
                looseGen.maximumSize = CGSize(width: maxWidth, height: max(1, scaledH))
            }
            do {
                cgImage = try looseGen.copyCGImage(at: cmTime, actualTime: &actualCM)
            } catch {
                errMsg = "\(error.localizedDescription)"
            }
        }

        guard let img = cgImage else {
            failures.append([
                "index": idx + 1,
                "requested_sec": (reqSec * 1000.0).rounded() / 1000.0,
                "error": errMsg ?? "Unknown decode failure",
            ])
            continue
        }

        let actualSec = CMTimeGetSeconds(actualCM)
        let displaySec = (actualSec.isFinite && actualSec >= 0) ? actualSec : reqSec
        let filename = String(format: "frame_%04d_t%06.2fs.png", idx + 1, displaySec)
        let dstURL = outURL.appendingPathComponent(filename)
        do {
            let sizeBytes = try writePNGAtomically(img, to: dstURL)
            extracted.append([
                "index": idx + 1,
                "requested_sec": (reqSec * 1000.0).rounded() / 1000.0,
                "timestamp_sec": (displaySec * 1000.0).rounded() / 1000.0,
                "path": dstURL.path,
                "width": img.width,
                "height": img.height,
                "size_bytes": sizeBytes,
            ])
        } catch {
            failures.append([
                "index": idx + 1,
                "requested_sec": (reqSec * 1000.0).rounded() / 1000.0,
                "error": "\(error)",
            ])
        }
    }

    let payload: [String: Any] = [
        "ok": !extracted.isEmpty,
        "input": inURL.path,
        "out_dir": outURL.path,
        "duration_sec": (durationSec * 1000.0).rounded() / 1000.0,
        "source_width": srcW,
        "source_height": srcH,
        "interval_sec": intervalSec,
        "extracted_count": extracted.count,
        "failure_count": failures.count,
        "frames": extracted,
        "failures": failures,
    ]
    if jsonOutput {
        emitJSON(payload)
    } else {
        print("Extracted \(extracted.count) frames (failures=\(failures.count)) from \(inURL.lastPathComponent) (\(String(format: "%.2f", durationSec))s)")
    }
    return extracted.isEmpty ? 1 : 0
}

private func cmdTranscode(_ args: [String]) -> Int32 {
    var inputPath: String?
    var outputPath: String?
    var presetArg = "1280x720"
    var jsonOutput = false

    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "--input":
            guard i + 1 < args.count else { return 2 }
            inputPath = args[i + 1]; i += 2
        case "--output":
            guard i + 1 < args.count else { return 2 }
            outputPath = args[i + 1]; i += 2
        case "--preset":
            guard i + 1 < args.count else { return 2 }
            presetArg = args[i + 1].lowercased(); i += 2
        case "--json":
            jsonOutput = true; i += 1
        default:
            fputs("Error: unknown transcode argument '\(a)'\n", stderr)
            return 2
        }
    }

    guard let inPath = inputPath, let outPath = outputPath else {
        fputs("Error: transcode requires --input <in> --output <out>\n", stderr)
        return 2
    }

    let inURL = URL(fileURLWithPath: inPath)
    let outURL = URL(fileURLWithPath: outPath)
    let fm = FileManager.default
    try? fm.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    if fm.fileExists(atPath: outURL.path) {
        try? fm.removeItem(at: outURL)
    }

    let asset = AVURLAsset(url: inURL)
    let presetMap: [String: String] = [
        "1280x720": AVAssetExportPreset1280x720,
        "720p": AVAssetExportPreset1280x720,
        "1920x1080": AVAssetExportPreset1920x1080,
        "1080p": AVAssetExportPreset1920x1080,
        "high": AVAssetExportPresetHighestQuality,
        "medium": AVAssetExportPresetMediumQuality,
        "passthrough": AVAssetExportPresetPassthrough,
    ]
    let desiredPreset = presetMap[presetArg] ?? AVAssetExportPreset1280x720
    let compatible = AVAssetExportSession.exportPresets(compatibleWith: asset)
    let chosenPreset: String
    if compatible.contains(desiredPreset) {
        chosenPreset = desiredPreset
    } else if compatible.contains(AVAssetExportPresetHighestQuality) {
        chosenPreset = AVAssetExportPresetHighestQuality
    } else if let first = compatible.first {
        chosenPreset = first
    } else {
        let payload: [String: Any] = ["ok": false, "error": "No compatible AVAssetExportSession presets"]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: no compatible export presets\n", stderr) }
        return 1
    }

    guard let session = AVAssetExportSession(asset: asset, presetName: chosenPreset) else {
        let payload: [String: Any] = ["ok": false, "error": "Failed to create AVAssetExportSession"]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: failed to create export session\n", stderr) }
        return 1
    }

    session.outputURL = outURL
    session.outputFileType = .mp4
    session.shouldOptimizeForNetworkUse = true

    let sem = DispatchSemaphore(value: 0)
    session.exportAsynchronously {
        sem.signal()
    }
    sem.wait()

    let ok = session.status == .completed && fm.fileExists(atPath: outURL.path)
    var outSize = 0
    if ok, let attrs = try? fm.attributesOfItem(atPath: outURL.path) {
        outSize = (attrs[.size] as? NSNumber)?.intValue ?? 0
    }
    var payload: [String: Any] = [
        "ok": ok,
        "input": inURL.path,
        "output": outURL.path,
        "preset": chosenPreset,
        "output_size_bytes": outSize,
    ]
    if let err = session.error {
        payload["error"] = err.localizedDescription
    }
    if jsonOutput {
        emitJSON(payload)
    } else {
        print("transcode ok=\(ok) preset=\(chosenPreset) size=\(outSize)")
    }
    return ok ? 0 : 1
}

private func cmdFindWindow(_ args: [String]) -> Int32 {
    var targetPid: Int32?
    var ownerFilter: String?
    var jsonOutput = false

    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "--pid":
            guard i + 1 < args.count, let v = Int32(args[i + 1]) else { return 2 }
            targetPid = v; i += 2
        case "--owner":
            guard i + 1 < args.count else { return 2 }
            ownerFilter = args[i + 1].lowercased(); i += 2
        case "--json":
            jsonOutput = true; i += 1
        default:
            fputs("Error: unknown find-window argument '\(a)'\n", stderr)
            return 2
        }
    }

    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        let payload: [String: Any] = ["ok": false, "windows": [], "error": "CGWindowListCopyWindowInfo returned nil"]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: could not list windows\n", stderr) }
        return 1
    }

    var matches: [[String: Any]] = []
    for entry in list {
        let pid = (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? -1
        let owner = (entry[kCGWindowOwnerName as String] as? String) ?? ""
        let title = (entry[kCGWindowName as String] as? String) ?? ""
        let wid = (entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
        let layer = (entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        let boundsDict = entry[kCGWindowBounds as String] as? [String: Any] ?? [:]
        let w = (boundsDict["Width"] as? NSNumber)?.doubleValue ?? 0
        let h = (boundsDict["Height"] as? NSNumber)?.doubleValue ?? 0
        let x = (boundsDict["X"] as? NSNumber)?.doubleValue ?? 0
        let y = (boundsDict["Y"] as? NSNumber)?.doubleValue ?? 0

        if layer != 0 || w < 100 || h < 100 {
            continue
        }
        if let tp = targetPid, pid != tp {
            continue
        }
        if let of = ownerFilter, !owner.lowercased().contains(of) {
            continue
        }
        matches.append([
            "window_id": wid,
            "pid": Int(pid),
            "owner": owner,
            "title": title,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "area": w * h,
        ])
    }

    matches.sort { (($0["area"] as? Double) ?? 0) > (($1["area"] as? Double) ?? 0) }
    let best = matches.first
    var payload: [String: Any] = [
        "ok": best != nil,
        "count": matches.count,
        "windows": matches,
    ]
    if let b = best {
        payload["best_window"] = b
        payload["window_id"] = b["window_id"]
    }
    if jsonOutput {
        emitJSON(payload)
    } else if let b = best {
        print("window_id=\(b["window_id"] ?? 0) owner=\(b["owner"] ?? "") \(b["width"] ?? 0)x\(b["height"] ?? 0)")
    } else {
        print("No matching on-screen window found")
    }
    return best != nil ? 0 : 1
}

private func cmdSynthVideo(_ args: [String]) -> Int32 {
    var outputPath: String?
    var durationSec = 4.0
    var fps = 15
    var width = 320
    var height = 180
    var blackHeadSec = 0.0
    var staticTailSec = 0.0
    var clipAtSec: Double?
    var amberAtSec: Double?
    var jsonOutput = false

    var i = 0
    while i < args.count {
        let a = args[i]
        switch a {
        case "--output":
            guard i + 1 < args.count else { return 2 }
            outputPath = args[i + 1]; i += 2
        case "--duration":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v > 0 else { return 2 }
            durationSec = v; i += 2
        case "--fps":
            guard i + 1 < args.count, let v = Int(args[i + 1]), v > 0 else { return 2 }
            fps = v; i += 2
        case "--width":
            guard i + 1 < args.count, let v = Int(args[i + 1]), v >= 64 else { return 2 }
            width = v; i += 2
        case "--height":
            guard i + 1 < args.count, let v = Int(args[i + 1]), v >= 64 else { return 2 }
            height = v; i += 2
        case "--black-head":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v >= 0 else { return 2 }
            blackHeadSec = v; i += 2
        case "--static-tail":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v >= 0 else { return 2 }
            staticTailSec = v; i += 2
        case "--clip-at":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v >= 0 else { return 2 }
            clipAtSec = v; i += 2
        case "--amber-at":
            guard i + 1 < args.count, let v = Double(args[i + 1]), v >= 0 else { return 2 }
            amberAtSec = v; i += 2
        case "--json":
            jsonOutput = true; i += 1
        default:
            fputs("Error: unknown synth-video argument '\(a)'\n", stderr)
            return 2
        }
    }

    guard let outPath = outputPath else {
        fputs("Error: synth-video requires --output <out.mp4>\n", stderr)
        return 2
    }

    let outURL = URL(fileURLWithPath: outPath)
    let fm = FileManager.default
    try? fm.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    if fm.fileExists(atPath: outURL.path) {
        try? fm.removeItem(at: outURL)
    }

    do {
        let writer = try AVAssetWriter(outputURL: outURL, fileType: .mp4)
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = false

        let bufferAttrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height,
        ]
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: input,
            sourcePixelBufferAttributes: bufferAttrs
        )
        guard writer.canAdd(input) else {
            throw ToolError("Cannot add video writer input")
        }
        writer.add(input)
        guard writer.startWriting() else {
            throw ToolError("AVAssetWriter failed to start: \(writer.error?.localizedDescription ?? "unknown")")
        }
        writer.startSession(atSourceTime: .zero)

        let totalFrames = max(2, Int(round(durationSec * Double(fps))))
        let staticStartSec = max(blackHeadSec, durationSec - staticTailSec)

        for frameIdx in 0..<totalFrames {
            while !input.isReadyForMoreMediaData {
                usleep(2000)
            }
            var pbOpt: CVPixelBuffer?
            let status = CVPixelBufferPoolCreatePixelBuffer(
                kCFAllocatorDefault,
                adaptor.pixelBufferPool!,
                &pbOpt
            )
            guard status == kCVReturnSuccess, let pb = pbOpt else {
                throw ToolError("Failed to allocate CVPixelBuffer (status=\(status))")
            }

            let tSec = Double(frameIdx) / Double(fps)
            let motionT = (staticTailSec > 0 && tSec >= staticStartSec) ? staticStartSec : tSec

            CVPixelBufferLockBaseAddress(pb, [])
            if let base = CVPixelBufferGetBaseAddress(pb) {
                let bytesPerRow = CVPixelBufferGetBytesPerRow(pb)
                let ptr = base.assumingMemoryBound(to: UInt8.self)

                let isBlack = tSec < blackHeadSec
                let isClip = (clipAtSec != nil) && abs(tSec - clipAtSec!) < 0.35
                let isAmber = (amberAtSec != nil) && abs(tSec - amberAtSec!) < 0.35

                let shiftX = Int(motionT * 70.0) % max(1, width)
                let carX = Int((Double(width) * 0.15) + (motionT * 55.0).truncatingRemainder(dividingBy: Double(width) * 0.65))

                for y in 0..<height {
                    let row = ptr + y * bytesPerRow
                    for x in 0..<width {
                        var r: UInt8 = 0
                        var g: UInt8 = 0
                        var b: UInt8 = 0
                        if !isBlack {
                            if isClip {
                                r = 254; g = 254; b = 254
                            } else if isAmber {
                                r = 225; g = 140; b = 55
                                if ((x + shiftX) / 16) % 2 == 0 {
                                    r = 185; g = 110; b = 45
                                }
                            } else {
                                // Sky top half, moving road & car bottom half
                                if y < height / 2 {
                                    r = 105; g = 150; b = 205
                                    if ((x + shiftX) / 24) % 3 == 0 && y > height / 6 && y < height / 3 {
                                        r = 220; g = 228; b = 236
                                    }
                                } else {
                                    // Asphalt with scrolling lane markings
                                    let stripe = ((x + shiftX * 2) / 20 + (y / 14)) % 2 == 0
                                    r = stripe ? 88 : 58
                                    g = stripe ? 92 : 62
                                    b = stripe ? 98 : 68
                                    // Moving colored car rectangle
                                    if x >= carX && x < carX + 44 && y >= Int(Double(height) * 0.62) && y < Int(Double(height) * 0.84) {
                                        r = 215; g = 65; b = 55
                                    }
                                }
                            }
                        }
                        let off = x * 4
                        row[off + 0] = b
                        row[off + 1] = g
                        row[off + 2] = r
                        row[off + 3] = 255
                    }
                }
            }
            CVPixelBufferUnlockBaseAddress(pb, [])

            let pts = CMTime(value: CMTimeValue(frameIdx), timescale: CMTimeScale(fps))
            if !adaptor.append(pb, withPresentationTime: pts) {
                throw ToolError("Failed to append frame \(frameIdx): \(writer.error?.localizedDescription ?? "unknown")")
            }
        }

        input.markAsFinished()
        let sem = DispatchSemaphore(value: 0)
        writer.finishWriting {
            sem.signal()
        }
        sem.wait()

        guard writer.status == .completed else {
            throw ToolError("AVAssetWriter finish failed: \(writer.error?.localizedDescription ?? "unknown")")
        }

        let attrs = try fm.attributesOfItem(atPath: outURL.path)
        let sizeBytes = (attrs[.size] as? NSNumber)?.intValue ?? 0
        let payload: [String: Any] = [
            "ok": true,
            "output": outURL.path,
            "duration_sec": durationSec,
            "fps": fps,
            "width": width,
            "height": height,
            "frames_written": totalFrames,
            "file_size_bytes": sizeBytes,
        ]
        if jsonOutput {
            emitJSON(payload)
        } else {
            print("Synthesized \(outURL.path) (\(totalFrames) frames, \(sizeBytes) bytes)")
        }
        return 0
    } catch {
        let payload: [String: Any] = ["ok": false, "error": "\(error)"]
        if jsonOutput { emitJSON(payload) } else { fputs("Error: \(error)\n", stderr) }
        return 1
    }
}

private func mainRun() -> Int32 {
    let args = Array(CommandLine.arguments.dropFirst())
    guard let sub = args.first else {
        printUsage()
        return 0
    }
    if sub == "-h" || sub == "--help" {
        printUsage()
        return 0
    }
    let rest = Array(args.dropFirst())
    switch sub {
    case "probe":
        return cmdProbe(rest)
    case "extract":
        return cmdExtract(rest)
    case "transcode":
        return cmdTranscode(rest)
    case "find-window":
        return cmdFindWindow(rest)
    case "synth-video":
        return cmdSynthVideo(rest)
    default:
        fputs("Error: unknown subcommand '\(sub)'\n", stderr)
        printUsage()
        return 2
    }
}

exit(mainRun())
