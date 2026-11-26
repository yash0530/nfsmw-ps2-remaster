// macos_key_driver.swift — Pure Apple AppKit + CoreGraphics keyboard/chord driver for PCSX2.
//
// Compiles cleanly with:
//   swiftc -O tools/macos_key_driver.swift -o build/flywheel/key_driver
//
// Supported actions (comma-separated or repeated --actions):
//   tap:<key>:<ms>        Press key, sleep <ms>, release key (default ms=120 if omitted)
//   hold:<k1+k2>:<ms>     Press chord keys simultaneously, sleep <ms>, release in reverse order
//   wait:<ms>             Sleep <ms>
//   f8                    Press virtualKey 100 for 90ms, release, wait 200ms (PCSX2 F8 screenshot)
//
// Persistent streaming mode (--stdin):
//   Reads newline-delimited commands from stdin for low-latency closed-loop driving:
//     set <k1+k2|none>    Diff currently held keys and press/release only deltas
//     release             Release all currently held keys
//     tap <k1+k2> [ms]    Press & release chord for ms (default 120)
//     f8                  Trigger F8 screenshot
//     focus               Re-focus target PID window
//     quit                Release all held keys and exit cleanly

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

private let keyMap: [String: CGKeyCode] = [
    "w": 13,
    "a": 0,
    "s": 1,
    "d": 2,
    "k": 40,
    "l": 37,
    "i": 34,
    "j": 38,
    "g": 5,
    "h": 4,
    "q": 12,
    "e": 14,
    "p": 35,
    "v": 9,
    "o": 31,
    "r": 15,
    "return": 36,
    "enter": 36,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "space": 49,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "1": 18,
    "2": 19,
    "3": 20,
    "esc": 53,
    "escape": 53,
    "tab": 48,
]

enum TapMode: String {
    case auto
    case hid
    case session
    case pid
}

enum DriverAction {
    case tap(keys: [String], codes: [CGKeyCode], ms: UInt32)
    case hold(keys: [String], codes: [CGKeyCode], ms: UInt32)
    case wait(ms: UInt32)
    case f8
    case f12

    var description: String {
        switch self {
        case .tap(let keys, _, let ms):
            return "tap:\(keys.joined(separator: "+")):\(ms)"
        case .hold(let keys, _, let ms):
            return "hold:\(keys.joined(separator: "+")):\(ms)"
        case .wait(let ms):
            return "wait:\(ms)"
        case .f8:
            return "f8"
        case .f12:
            return "f12"
        }
    }
}

struct DriverError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) {
        self.description = message
    }
}

private func printUsage() {
    let help = """
    Usage: key_driver [--pid <pid>] --actions "<action_list>" [options]

    Options:
      --pid <pid>            Target process ID to focus and send keyboard events to
      --actions <list>       Comma-separated actions (may be repeated):
                               tap:<key>:<ms>     Press & release key after <ms>
                               hold:<k1+k2>:<ms>  Hold chord simultaneously for <ms>
                               wait:<ms>          Sleep for <ms>
                               f8                 Press F8 (vk 100) for 90ms, wait 200ms
                               f12                Press F12 (vk 111) for 90ms, wait 200ms
      --tap-mode <mode>      Event tap target: auto | hid | session | pid (default: auto)
      --focus-wait-ms <ms>   Delay after activating target app before sending keys (default: 120)
      --stdin                Persistent line-buffered stdin command mode (set/release/tap/f8/focus/quit)
      --check-accessibility  Print AXIsProcessTrusted() status and exit
      --dry-run              Parse & validate actions without activating or posting events
      --json                 Emit structured JSON output
      -h, --help             Show this help text and exit

    Supported keys:
      w, a, s, d, k, l, i, j, g, h, q, e, p, v, o, r, return, f8, f9, f10, f11, f12, space, up, down, left, right, 1, 2, 3
    """
    print(help)
}

private func resolveKeys(_ spec: String) throws -> ([String], [CGKeyCode]) {
    let parts = spec
        .split(separator: "+")
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
        .filter { !$0.isEmpty }
    guard !parts.isEmpty else {
        throw DriverError("Empty key specification '\(spec)'")
    }
    var codes: [CGKeyCode] = []
    for key in parts {
        guard let code = keyMap[key] else {
            throw DriverError("Unknown key '\(key)' in '\(spec)'")
        }
        codes.append(code)
    }
    return (parts, codes)
}

private func parseDurationMs(_ raw: String, context: String) throws -> UInt32 {
    guard let value = UInt32(raw.trimmingCharacters(in: .whitespacesAndNewlines)) else {
        throw DriverError("Invalid millisecond duration '\(raw)' in '\(context)'")
    }
    return value
}

private func parseActionToken(_ token: String) throws -> DriverAction {
    let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty {
        throw DriverError("Empty action token")
    }
    let lower = trimmed.lowercased()
    if lower == "f8" {
        return .f8
    }
    if lower == "f12" {
        return .f12
    }
    let parts = lower.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
    guard let verb = parts.first else {
        throw DriverError("Invalid action '\(trimmed)'")
    }
    switch verb {
    case "wait":
        guard parts.count == 2 else {
            throw DriverError("wait action requires wait:<ms>, got '\(trimmed)'")
        }
        return .wait(ms: try parseDurationMs(parts[1], context: trimmed))
    case "tap":
        guard parts.count == 2 || parts.count == 3 else {
            throw DriverError("tap action requires tap:<key>:<ms>, got '\(trimmed)'")
        }
        let (keys, codes) = try resolveKeys(parts[1])
        let ms: UInt32 = parts.count == 3 ? try parseDurationMs(parts[2], context: trimmed) : 120
        return .tap(keys: keys, codes: codes, ms: ms)
    case "hold":
        guard parts.count == 3 else {
            throw DriverError("hold action requires hold:<k1+k2>:<ms>, got '\(trimmed)'")
        }
        let (keys, codes) = try resolveKeys(parts[1])
        let ms = try parseDurationMs(parts[2], context: trimmed)
        return .hold(keys: keys, codes: codes, ms: ms)
    default:
        throw DriverError("Unknown action '\(verb)' in '\(trimmed)'")
    }
}

private func sleepMs(_ ms: UInt32) {
    if ms > 0 {
        usleep(useconds_t(ms) * 1000)
    }
}

private func postKeyEvent(
    keyCode: CGKeyCode,
    keyDown: Bool,
    mode: TapMode,
    pid: pid_t,
    source: CGEventSource?
) throws {
    guard let event = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: keyDown) else {
        throw DriverError("Failed to create CGEvent for virtualKey=\(keyCode) keyDown=\(keyDown)")
    }
    // Keep modifier flags clean so external Shift/Command state never turns F8 into Shift+F8.
    event.flags = []
    switch mode {
    case .auto, .hid:
        event.post(tap: .cghidEventTap)
    case .session:
        event.post(tap: .cgSessionEventTap)
    case .pid:
        event.postToPid(pid)
    }
}

private func focusProcess(pid: pid_t, waitMs: UInt32) -> (found: Bool, activated: Bool, bundleId: String?) {
    guard let app = NSRunningApplication(processIdentifier: pid) else {
        return (false, false, nil)
    }
    if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid {
        return (true, true, app.bundleIdentifier)
    }
    var activated: Bool
    if #available(macOS 14.0, *) {
        activated = app.activate(options: [.activateAllWindows])
    } else {
        activated = app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
    }
    sleepMs(waitMs)
    // If cooperative activation did not make target frontmost, fall back to System Events.
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != pid {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = [
            "-e",
            "tell application \"System Events\" to set frontmost of (first process whose unix id is \(pid)) to true",
        ]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()
        sleepMs(min(waitMs, 120))
        activated = (NSWorkspace.shared.frontmostApplication?.processIdentifier == pid) || activated
    }
    return (true, activated, app.bundleIdentifier)
}

@discardableResult
private func ensureFocused(pid: pid_t, waitMs: UInt32 = 80) -> Bool {
    if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid {
        return false
    }
    _ = focusProcess(pid: pid, waitMs: waitMs)
    return true
}

private func emitJSON(_ obj: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
}

private func run() -> Int32 {
    let args = Array(CommandLine.arguments.dropFirst())
    if args.isEmpty || args.contains("-h") || args.contains("--help") {
        printUsage()
        return 0
    }

    var pidValue: Int32?
    var actionLists: [String] = []
    var tapMode: TapMode = .auto
    var focusWaitMs: UInt32 = 120
    var dryRun = false
    var jsonOutput = false
    var checkAccessibility = false
    var stdinMode = false

    var idx = 0
    while idx < args.count {
        let arg = args[idx]
        switch arg {
        case "--pid":
            guard idx + 1 < args.count, let parsed = Int32(args[idx + 1]) else {
                fputs("Error: --pid requires an integer PID\n", stderr)
                return 2
            }
            pidValue = parsed
            idx += 2
        case "--actions":
            guard idx + 1 < args.count else {
                fputs("Error: --actions requires a value\n", stderr)
                return 2
            }
            actionLists.append(args[idx + 1])
            idx += 2
        case "--tap-mode":
            guard idx + 1 < args.count, let mode = TapMode(rawValue: args[idx + 1].lowercased()) else {
                fputs("Error: --tap-mode must be auto, hid, session, or pid\n", stderr)
                return 2
            }
            tapMode = mode
            idx += 2
        case "--focus-wait-ms":
            guard idx + 1 < args.count, let ms = UInt32(args[idx + 1]) else {
                fputs("Error: --focus-wait-ms requires a non-negative integer\n", stderr)
                return 2
            }
            focusWaitMs = ms
            idx += 2
        case "--stdin":
            stdinMode = true
            idx += 1
        case "--dry-run":
            dryRun = true
            idx += 1
        case "--json":
            jsonOutput = true
            idx += 1
        case "--check-accessibility":
            checkAccessibility = true
            idx += 1
        default:
            fputs("Error: unknown argument '\(arg)'\n", stderr)
            return 2
        }
    }

    let accessibilityPermitted = AXIsProcessTrusted()

    if checkAccessibility {
        if jsonOutput {
            emitJSON(["accessibility_permitted": accessibilityPermitted])
        } else {
            print("accessibility_permitted=\(accessibilityPermitted)")
        }
        return 0
    }

    // In auto mode, prefer .cghidEventTap when Accessibility is granted, otherwise fall back to postToPid.
    let effectiveMode: TapMode = (tapMode == .auto) ? (accessibilityPermitted ? .hid : .pid) : tapMode

    if stdinMode {
        if dryRun {
            let payload: [String: Any] = [
                "ok": true,
                "dry_run": true,
                "stdin_mode": true,
                "pid": pidValue as Any,
                "accessibility_permitted": accessibilityPermitted,
                "tap_mode_requested": tapMode.rawValue,
                "tap_mode_effective": effectiveMode.rawValue,
            ]
            if jsonOutput {
                emitJSON(payload)
            } else {
                print("Dry-run OK: stdin mode, mode=\(effectiveMode.rawValue)")
            }
            return 0
        }

        guard let rawPid = pidValue else {
            fputs("Error: --pid <pid> is required for --stdin unless --dry-run is set\n", stderr)
            return 2
        }
        let targetPid = pid_t(rawPid)
        let focus = focusProcess(pid: targetPid, waitMs: focusWaitMs)
        if !focus.found {
            let msg = "No running NSRunningApplication found for PID \(rawPid)"
            fputs("Error: \(msg)\n", stderr)
            return 1
        }

        let eventSource = CGEventSource(stateID: .hidSystemState)
        var heldCodes: [CGKeyCode] = []

        func releaseAllHeld() {
            for code in heldCodes.reversed() {
                try? postKeyEvent(keyCode: code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
            }
            heldCodes.removeAll()
        }

        setbuf(stdout, nil)
        print("READY mode=\(effectiveMode.rawValue) ax=\(accessibilityPermitted)")

        while let rawLine = readLine() {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            if line.isEmpty {
                continue
            }
            let tokens = line.split(whereSeparator: { $0.isWhitespace }).map(String.init)
            guard let cmd = tokens.first?.lowercased() else { continue }

            do {
                switch cmd {
                case "quit", "exit":
                    releaseAllHeld()
                    print("BYE")
                    return 0
                case "release":
                    releaseAllHeld()
                case "focus":
                    _ = focusProcess(pid: targetPid, waitMs: min(focusWaitMs, 80))
                case "f8":
                    if effectiveMode != .pid {
                        _ = ensureFocused(pid: targetPid)
                    }
                    let f8Code: CGKeyCode = 100
                    try postKeyEvent(keyCode: f8Code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                    sleepMs(90)
                    try postKeyEvent(keyCode: f8Code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                case "set":
                    let spec = tokens.count >= 2 ? tokens[1].lowercased() : "none"
                    let desiredCodes: [CGKeyCode]
                    if spec == "none" || spec == "-" || spec == "clear" {
                        desiredCodes = []
                    } else {
                        let (_, codes) = try resolveKeys(spec)
                        desiredCodes = codes
                    }
                    let desiredSet = Set(desiredCodes)
                    let heldSet = Set(heldCodes)
                    if !desiredCodes.isEmpty && effectiveMode != .pid {
                        _ = ensureFocused(pid: targetPid)
                    }
                    // Release keys no longer desired
                    for code in heldCodes.reversed() where !desiredSet.contains(code) {
                        try postKeyEvent(keyCode: code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                    }
                    // Press newly desired keys
                    for code in desiredCodes where !heldSet.contains(code) {
                        try postKeyEvent(keyCode: code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                    }
                    heldCodes = desiredCodes
                case "tap":
                    guard tokens.count >= 2 else {
                        throw DriverError("tap requires <key> [ms]")
                    }
                    let (_, codes) = try resolveKeys(tokens[1])
                    let ms: UInt32 = tokens.count >= 3 ? try parseDurationMs(tokens[2], context: line) : 120
                    if effectiveMode != .pid {
                        _ = ensureFocused(pid: targetPid)
                    }
                    for code in codes {
                        try postKeyEvent(keyCode: code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                    }
                    sleepMs(ms)
                    for code in codes.reversed() {
                        try postKeyEvent(keyCode: code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                    }
                default:
                    fputs("WARN unknown stdin command '\(cmd)'\n", stderr)
                }
            } catch {
                fputs("WARN stdin error: \(error)\n", stderr)
            }
        }
        releaseAllHeld()
        return 0
    }

    var parsedActions: [DriverAction] = []
    do {
        for rawList in actionLists {
            for token in rawList.split(separator: ",") {
                let s = String(token).trimmingCharacters(in: .whitespacesAndNewlines)
                if !s.isEmpty {
                    parsedActions.append(try parseActionToken(s))
                }
            }
        }
        if parsedActions.isEmpty {
            throw DriverError("No actions provided (pass --actions \"...\")")
        }
    } catch {
        if jsonOutput {
            emitJSON([
                "ok": false,
                "error": "\(error)",
                "accessibility_permitted": accessibilityPermitted,
            ])
        } else {
            fputs("Error: \(error)\n", stderr)
        }
        return 2
    }

    if dryRun {
        let payload: [String: Any] = [
            "ok": true,
            "dry_run": true,
            "pid": pidValue as Any,
            "accessibility_permitted": accessibilityPermitted,
            "tap_mode_requested": tapMode.rawValue,
            "tap_mode_effective": effectiveMode.rawValue,
            "actions_count": parsedActions.count,
            "actions": parsedActions.map { $0.description },
        ]
        if jsonOutput {
            emitJSON(payload)
        } else {
            print("Dry-run OK: \(parsedActions.count) action(s), mode=\(effectiveMode.rawValue)")
        }
        return 0
    }

    guard let rawPid = pidValue else {
        fputs("Error: --pid <pid> is required unless --dry-run or --check-accessibility is set\n", stderr)
        return 2
    }
    let targetPid = pid_t(rawPid)

    let focus = focusProcess(pid: targetPid, waitMs: focusWaitMs)
    if !focus.found {
        let msg = "No running NSRunningApplication found for PID \(rawPid)"
        if jsonOutput {
            emitJSON([
                "ok": false,
                "error": msg,
                "pid": rawPid,
                "accessibility_permitted": accessibilityPermitted,
            ])
        } else {
            fputs("Error: \(msg)\n", stderr)
        }
        return 1
    }

    let eventSource = CGEventSource(stateID: .hidSystemState)
    var executed: [String] = []
    var refocusCount = 0

    do {
        for action in parsedActions {
            switch action {
            case .wait(let ms):
                sleepMs(ms)
            case .tap(_, let codes, let ms):
                if effectiveMode != .pid && ensureFocused(pid: targetPid) {
                    refocusCount += 1
                }
                for code in codes {
                    try postKeyEvent(keyCode: code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                }
                sleepMs(ms)
                for code in codes.reversed() {
                    try postKeyEvent(keyCode: code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                }
                sleepMs(35)
            case .hold(_, let codes, let ms):
                if effectiveMode != .pid && ensureFocused(pid: targetPid) {
                    refocusCount += 1
                }
                for code in codes {
                    try postKeyEvent(keyCode: code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                }
                sleepMs(ms)
                for code in codes.reversed() {
                    try postKeyEvent(keyCode: code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                }
                sleepMs(40)
            case .f8:
                if effectiveMode != .pid && ensureFocused(pid: targetPid) {
                    refocusCount += 1
                }
                let f8Code: CGKeyCode = 100
                try postKeyEvent(keyCode: f8Code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                sleepMs(90)
                try postKeyEvent(keyCode: f8Code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                sleepMs(200)
            case .f12:
                if effectiveMode != .pid && ensureFocused(pid: targetPid) {
                    refocusCount += 1
                }
                let f12Code: CGKeyCode = 111
                try postKeyEvent(keyCode: f12Code, keyDown: true, mode: effectiveMode, pid: targetPid, source: eventSource)
                sleepMs(90)
                try postKeyEvent(keyCode: f12Code, keyDown: false, mode: effectiveMode, pid: targetPid, source: eventSource)
                sleepMs(200)
            }
            executed.append(action.description)
        }
    } catch {
        if jsonOutput {
            emitJSON([
                "ok": false,
                "error": "\(error)",
                "pid": rawPid,
                "accessibility_permitted": accessibilityPermitted,
                "tap_mode_effective": effectiveMode.rawValue,
                "actions_executed": executed,
                "refocus_count": refocusCount,
            ])
        } else {
            fputs("Error: \(error)\n", stderr)
        }
        return 1
    }

    let frontmostPid = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1
    if jsonOutput {
        emitJSON([
            "ok": true,
            "pid": rawPid,
            "bundle_id": focus.bundleId ?? "",
            "focused": focus.activated,
            "frontmost_pid": frontmostPid,
            "refocus_count": refocusCount,
            "accessibility_permitted": accessibilityPermitted,
            "tap_mode_requested": tapMode.rawValue,
            "tap_mode_effective": effectiveMode.rawValue,
            "actions_executed": executed,
        ])
    } else {
        print("Executed \(executed.count) action(s) on PID \(rawPid) (mode=\(effectiveMode.rawValue), ax=\(accessibilityPermitted), refocus=\(refocusCount))")
    }
    return 0
}

exit(run())
