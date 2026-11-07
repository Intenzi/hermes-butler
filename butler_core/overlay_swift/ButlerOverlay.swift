//
//  ButlerOverlay.swift
//  Companion overlay renderer for the butler guide.
//
//  A single-file AppKit command-line app. It reads newline-delimited JSON ops
//  from stdin and draws click-through, non-activating overlays above every
//  other window. It is compiled on demand with:
//
//      swiftc -O -o butler_core/services/ButlerOverlay \
//          butler_core/overlay_swift/ButlerOverlay.swift
//
//  Protocol (one JSON object per line, EOF-terminated):
//    {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":n,"total":m}
//    {"op":"chip","step":n}
//    {"op":"pet","frames_dir":"...","scale":0.33,"pet_enabled":true}
//    {"op":"launcher"}
//    {"op":"done"}
//    {"op":"axtrusted","prompt":true}
//    {"op":"axcapture","target":null,"max_elements":150,"depth":10}
//    {"op":"idle"}
//    {"op":"quit"}
//
//  Coordinates in "box" are global Accessibility (Quartz) screen coordinates:
//  origin at the top-left of the primary display, y increasing downward.
//
//  The renderer tears down its windows on EOF, "quit", SIGINT, and SIGTERM.
//

import AppKit
import ApplicationServices
import CoreGraphics

// MARK: - Logging

private func logStderr(_ message: String) {
    guard let data = (message + "\n").data(using: .utf8) else {
        return
    }
    FileHandle.standardError.write(data)
}

// Malformed input is noisy by nature (a broken producer can repeat the same
// line forever), so each distinct malformed line is reported once.
private let malformedLogLock = NSLock()
private var malformedLogKeys = Set<String>()
private let malformedLogKeyLimit = 4096

private func logMalformedOnce(line: String, reason: String) {
    malformedLogLock.lock()
    let shouldLog: Bool
    if malformedLogKeys.contains(line) {
        shouldLog = false
    } else if malformedLogKeys.count >= malformedLogKeyLimit {
        shouldLog = false
    } else {
        malformedLogKeys.insert(line)
        shouldLog = true
    }
    malformedLogLock.unlock()

    guard shouldLog else {
        return
    }
    let snippet: String
    if line.count > 200 {
        snippet = String(line.prefix(200)) + "..."
    } else {
        snippet = line
    }
    logStderr("butler-overlay: malformed op dropped (\(reason)): \(snippet)")
}

// A bad pet directory is a configuration problem rather than a malformed op, so
// report it once per directory and then keep the rest of the overlay alive.
private let petLogLock = NSLock()
private var petLogKeys = Set<String>()

private func logPetFailureOnce(framesDir: String, reason: String) {
    petLogLock.lock()
    let shouldLog = petLogKeys.insert(framesDir).inserted
    petLogLock.unlock()

    guard shouldLog else {
        return
    }
    logStderr("butler-overlay: pet unavailable (\(reason)): \(framesDir)")
}

// MARK: - JSON value helpers

private func numberValue(_ value: Any?) -> Double? {
    guard let value = value, !(value is NSNull) else {
        return nil
    }
    if let number = value as? NSNumber {
        // JSON booleans bridge to NSNumber too; they are not valid geometry.
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        let double = number.doubleValue
        return double.isFinite ? double : nil
    }
    if let double = value as? Double {
        return double.isFinite ? double : nil
    }
    if let int = value as? Int {
        return Double(int)
    }
    return nil
}

private func intValue(_ value: Any?) -> Int? {
    guard let value = value, !(value is NSNull) else {
        return nil
    }
    if let number = value as? NSNumber {
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        return intValue(fromDouble: number.doubleValue)
    }
    if let int = value as? Int {
        return int
    }
    if let double = value as? Double {
        return intValue(fromDouble: double)
    }
    return nil
}

private func intValue(fromDouble double: Double) -> Int? {
    // Keep JSON's arbitrary-precision numbers from trapping in Int(double).
    let upperBound = 9_223_372_036_854_775_808.0 // 2^63
    guard double.isFinite,
          double >= Double(Int.min),
          double < upperBound else {
        return nil
    }
    return Int(double)
}

private func stringValue(_ value: Any?) -> String? {
    guard let value = value, !(value is NSNull) else {
        return nil
    }
    return value as? String
}

/// Optional string fields accept absent or null. Any other type is malformed.
private func optionalStringField(_ object: [String: Any],
                                 key: String) -> (valid: Bool, value: String?) {
    guard let raw = object[key] else {
        return (true, nil)
    }
    if raw is NSNull {
        return (true, nil)
    }
    guard let text = raw as? String else {
        return (false, nil)
    }
    return (true, text)
}

// MARK: - Screen geometry

// AppKit global coordinates have their origin at the bottom-left of the
// primary display with y increasing upward. Quartz global coordinates (used by
// Accessibility and by the op protocol) have their origin at the top-left of
// the primary display with y increasing downward. The primary display anchors
// both coordinate spaces, including for secondary displays.
private func primaryScreen() -> NSScreen? {
    let screens = NSScreen.screens
    if let anchored = screens.first(where: {
        abs($0.frame.origin.x) < 0.5 && abs($0.frame.origin.y) < 0.5
    }) {
        return anchored
    }
    return screens.first
}

private func primaryScreenHeight() -> CGFloat? {
    guard let screen = primaryScreen(), screen.frame.height > 0.0 else {
        return nil
    }
    return screen.frame.height
}

private func appKitRect(fromQuartz rect: CGRect, primaryHeight: CGFloat) -> NSRect {
    return NSRect(x: rect.origin.x,
                  y: primaryHeight - rect.maxY,
                  width: rect.width,
                  height: rect.height)
}

private func quartzRect(fromAppKit rect: NSRect, primaryHeight: CGFloat) -> CGRect {
    return CGRect(x: rect.origin.x,
                  y: primaryHeight - rect.maxY,
                  width: rect.width,
                  height: rect.height)
}

/// The visible portion of the overlay canvas on one display, expressed as an
/// AppKit window frame plus the Quartz origin the view should draw against.
private struct ScreenSlice {
    let frame: NSRect
    let contentOrigin: CGPoint
}

private func visibleSlices(forQuartz canvas: CGRect,
                           primaryHeight: CGFloat) -> [ScreenSlice] {
    var slices: [ScreenSlice] = []

    for screen in NSScreen.screens {
        // Clamp each window to the screen's visibleFrame, not its full frame:
        // menu bars, the Dock, and notches never get painted over.
        let visibleQuartz = quartzRect(fromAppKit: screen.visibleFrame,
                                       primaryHeight: primaryHeight)
        let clipped = canvas.intersection(visibleQuartz)
        guard !clipped.isNull, clipped.width >= 1.0, clipped.height >= 1.0 else {
            continue
        }

        let frame = appKitRect(fromQuartz: clipped, primaryHeight: primaryHeight)
        let clamped = frame.intersection(screen.visibleFrame)
        guard !clamped.isNull, clamped.width >= 1.0, clamped.height >= 1.0 else {
            continue
        }
        slices.append(ScreenSlice(frame: clamped, contentOrigin: clipped.origin))
    }

    return slices
}

private func boxIntersectsAnyScreen(_ box: CGRect, primaryHeight: CGFloat) -> Bool {
    return NSScreen.screens.contains { screen in
        let screenQuartz = quartzRect(fromAppKit: screen.frame,
                                      primaryHeight: primaryHeight)
        return screenQuartz.intersects(box)
    }
}

private func textSize(_ text: String,
                      font: NSFont,
                      maxWidth: CGFloat,
                      paragraphStyle: NSParagraphStyle?) -> CGSize {
    var attributes: [NSAttributedString.Key: Any] = [.font: font]
    if let paragraphStyle = paragraphStyle {
        attributes[.paragraphStyle] = paragraphStyle
    }
    let bounds = (text as NSString).boundingRect(
        with: CGSize(width: maxWidth, height: CGFloat.greatestFiniteMagnitude),
        options: [.usesLineFragmentOrigin, .usesFontLeading],
        attributes: attributes
    )
    return CGSize(width: ceil(bounds.width), height: ceil(bounds.height))
}

private func makeParagraphStyle(lineBreakMode: NSLineBreakMode) -> NSParagraphStyle {
    let style = NSMutableParagraphStyle()
    style.lineBreakMode = lineBreakMode
    style.alignment = .left
    return style
}

// MARK: - Accessibility capture

// This is a 1:1 port of the Python AX walk that used to live in capture.py.
// The companion binary is the single TCC grant subject, so AX calls happen
// here, on the same process that already draws rings/chips/pets.

private let axDefaultMaxElements = 150
private let axDefaultDepth = 10

private struct AXCaptureSpec {
    let target: String?
    let maxElements: Int
    let depth: Int
}

private func copyAXAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    // AXError.apiDisabled (252) and every other OS error degrade to "missing".
    // The capture path never raises.
    guard error == .success, let value = value else {
        return nil
    }
    return value
}

private func axElement(from raw: Any?) -> AXUIElement? {
    guard let raw = raw else {
        return nil
    }
    // unsafeBitCast needs the 8-byte object reference; casting the 16-byte
    // Any existential directly would trap.
    let object = raw as AnyObject
    guard CFGetTypeID(object) == AXUIElementGetTypeID() else {
        return nil
    }
    return unsafeBitCast(object, to: AXUIElement.self)
}

private func axString(_ element: AXUIElement, _ name: String) -> String? {
    return copyAXAttribute(element, name) as? String
}

private func axInt(_ value: CGFloat) -> Int? {
    // Accessibility coordinates are small; reject non-finite/absurd values
    // instead of trapping in Int(CGFloat).
    let safeLimit = 9_007_199_254_740_992.0 // 2^53
    guard value.isFinite, value > -safeLimit, value < safeLimit else {
        return nil
    }
    return Int(value) // truncates toward zero, like Python int()
}

private func axPointOrSize(_ raw: Any?, kind: String) -> [Int]? {
    guard let raw = raw else {
        return nil
    }
    let object = raw as AnyObject
    guard CFGetTypeID(object) == AXValueGetTypeID() else {
        return nil
    }
    let value: AXValue = unsafeBitCast(object, to: AXValue.self)
    let type = AXValueGetType(value)
    if type == .cgPoint && kind == "point" {
        var point = CGPoint.zero
        guard AXValueGetValue(value, .cgPoint, &point),
              let x = axInt(point.x), let y = axInt(point.y) else {
            return nil
        }
        return [x, y]
    }
    if type == .cgSize && kind == "size" {
        var size = CGSize.zero
        guard AXValueGetValue(value, .cgSize, &size),
              let width = axInt(size.width), let height = axInt(size.height) else {
            return nil
        }
        return [width, height]
    }
    return nil
}

private struct AXFields {
    let role: String
    let label: String?
    let box: [Any]
}

private func readAXFields(_ element: AXUIElement) -> AXFields {
    let roleRaw = copyAXAttribute(element, "AXRole")
    let title = axString(element, "AXTitle")
    let description = axString(element, "AXDescription")
    let rawValue = copyAXAttribute(element, "AXValue")
    let position = copyAXAttribute(element, "AXPosition")
    let size = copyAXAttribute(element, "AXSize")

    var label: String?
    for candidate in [title, description] {
        guard let candidate = candidate else { continue }
        let trimmed = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            label = trimmed
            break
        }
    }
    if label == nil, let valueText = rawValue as? String {
        let trimmed = valueText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            label = String(trimmed.prefix(80))
        }
    }

    var box: [Any] = [NSNull(), NSNull(), NSNull(), NSNull()]
    if let point = axPointOrSize(position, kind: "point"),
       let dimensions = axPointOrSize(size, kind: "size"),
       point.count == 2, dimensions.count == 2 {
        box = [point[0], point[1], dimensions[0], dimensions[1]]
    }

    let role: String
    if let roleRaw = roleRaw {
        role = (roleRaw as? String) ?? String(describing: roleRaw)
    } else {
        role = "unknown"
    }
    return AXFields(role: role, label: label, box: box)
}

private func walkAX(_ element: AXUIElement,
                    depth: Int,
                    out: inout [[String: Any]],
                    budget: Int) {
    if depth < 0 || out.count >= budget {
        return
    }
    let fields = readAXFields(element)
    // read_fields always returns a box (possibly all-null), so every visited
    // element is recorded - matching the Python walk exactly.
    out.append(["role": fields.role,
                "label": fields.label ?? NSNull(),
                "box": fields.box])

    guard let rawKids = copyAXAttribute(element, "AXChildren"),
          let kids = rawKids as? [Any] else {
        return
    }
    for kid in kids {
        if out.count >= budget {
            return
        }
        if let kidElement = axElement(from: kid) {
            walkAX(kidElement, depth: depth - 1, out: &out, budget: budget)
        }
    }
}

private func axWindows(of root: AXUIElement) -> [AXUIElement] {
    if let focusedWindow = axElement(from: copyAXAttribute(root, "AXFocusedWindow")) {
        return [focusedWindow]
    }
    if let rawWindows = copyAXAttribute(root, "AXWindows"),
       let windows = rawWindows as? [Any] {
        return windows.compactMap { axElement(from: $0) }
    }
    return [root]
}

private func axWindowTitle(_ window: AXUIElement) -> String? {
    // Python: copy_attr(AXTitle) or copy_attr(AXDescription)
    if let title = axString(window, "AXTitle"), !title.isEmpty {
        return title
    }
    return axString(window, "AXDescription")
}

private struct AXRunningWindow {
    let pid: pid_t
    let name: String
    let title: String?
}

private func axTopmostLayer0Windows() -> [AXRunningWindow] {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let info = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return []
    }
    var out: [AXRunningWindow] = []
    for window in info {
        guard let layer = intValue(window[kCGWindowLayer as String]), layer == 0,
              let rawPid = intValue(window[kCGWindowOwnerPID as String]), rawPid > 0 else {
            continue
        }
        out.append(AXRunningWindow(
            pid: pid_t(rawPid),
            name: (window[kCGWindowOwnerName as String] as? String) ?? "",
            title: window[kCGWindowName as String] as? String))
    }
    return out
}

private func findRunningApp(_ target: String?) -> (pid: pid_t?, name: String?) {
    let windows = axTopmostLayer0Windows()
    if target == nil {
        if let first = windows.first {
            return (first.pid, first.name)
        }
    } else if let target = target {
        let needle = target.lowercased()
        for window in windows {
            if !window.name.isEmpty && window.name.lowercased().contains(needle) {
                return (window.pid, window.name)
            }
        }
    }

    let workspace = NSWorkspace.shared
    if target == nil {
        guard let app = workspace.frontmostApplication else {
            return (nil, nil)
        }
        return (app.processIdentifier, app.localizedName)
    }

    let needle = (target ?? "").lowercased()
    var candidates: [NSRunningApplication] = []
    for app in workspace.runningApplications {
        guard let name = app.localizedName, !name.isEmpty,
              name.lowercased().contains(needle), !app.isTerminated else {
            continue
        }
        candidates.append(app)
    }
    // Python sorts matches by app.windows() count, but NSRunningApplication
    // exposes no window list so that count is always 0; stable first-match is
    // the same observable behavior.
    guard let first = candidates.first else {
        return (nil, nil)
    }
    return (first.processIdentifier, first.localizedName)
}

private func axCaptureSnapshot(target: String?,
                               maxElements: Int,
                               depth: Int) -> [String: Any] {
    var result: [String: Any] = [
        "trusted": AXIsProcessTrusted(),
        "app": NSNull(),
        "window": NSNull(),
        "count": 0,
        "with_box": 0,
        "elements": [Any](),
    ]

    guard (result["trusted"] as? Bool) == true else {
        result["prompted"] = false
        result["note"] = "Accessibility not granted to this process"
        result["fix"] = "Grant Accessibility to \"Butler companion - the butler plugin's bundled helper\" in System Settings -> Privacy & Security -> Accessibility, then try the guide again. Because the butler reads the screen through its own small helper, macOS asks once and remembers across updates."
        return result
    }

    let found = findRunningApp(target)
    guard let pid = found.pid else {
        result["error"] = target == nil ? "no frontmost app" : "no running app matches target"
        return result
    }

    let root = AXUIElementCreateApplication(pid)
    result["app"] = found.name ?? NSNull()

    // Keyboard focus snapshot: the expect-engine's "focus" verdict.
    if let focusedElement = axElement(from: copyAXAttribute(root, "AXFocusedUIElement")) {
        let fields = readAXFields(focusedElement)
        if let label = fields.label {
            let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                result["focused"] = String(trimmed.prefix(120))
            }
        }
    }

    var elements: [[String: Any]] = []
    for window in axWindows(of: root) {
        if let title = axWindowTitle(window), result["window"] is NSNull {
            result["window"] = title
        }
        walkAX(window, depth: depth, out: &elements, budget: maxElements)
        if elements.count >= maxElements {
            break
        }
    }

    var withBox = 0
    for index in elements.indices {
        elements[index]["id"] = index
        if let box = elements[index]["box"] as? [Any], box.count == 4,
           !(box[0] is NSNull) {
            withBox += 1
        }
    }
    let limited = Array(elements.prefix(max(0, maxElements)))
    result["elements"] = limited
    result["count"] = limited.count
    result["with_box"] = withBox
    return result
}

private func axTrustedResponse(prompt: Bool) -> [String: Any] {
    var trusted = AXIsProcessTrusted()
    var prompted = false
    if !trusted && prompt {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        prompted = AXIsProcessTrustedWithOptions(options)
        if prompted {
            trusted = AXIsProcessTrusted()
        }
    }
    return ["trusted": trusted, "prompted": prompted]
}

private func writeAXResponse(_ response: [String: Any]) {
    let data: Data
    do {
        data = try JSONSerialization.data(withJSONObject: response, options: [])
    } catch {
        logStderr("butler-overlay: AX response encode failed: \(error.localizedDescription)")
        data = Data("{\"trusted\":false,\"note\":\"Butler companion capture failed\",\"fix\":\"Retry the guide.\"}".utf8)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
}

// MARK: - Parsed commands

private struct RingSpec {
    let box: CGRect
    let label: String?
    let say: String?
    let step: Int
    let total: Int
}

private struct PetSpec {
    let framesDir: String
    let scale: CGFloat
    let enabled: Bool
}

private enum OverlayCommand {
    case ring(RingSpec)
    case chip(Int)
    case pet(PetSpec)
    case launcher
    case done
    case axtrusted(prompt: Bool)
    case axcapture(AXCaptureSpec)
    case idle
    case quit
}

private func parseCommand(line: String) -> OverlayCommand? {
    guard let data = line.data(using: .utf8) else {
        logMalformedOnce(line: line, reason: "non-UTF8 input")
        return nil
    }

    let json: Any
    do {
        json = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
    } catch {
        logMalformedOnce(line: line, reason: "invalid JSON: \(error.localizedDescription)")
        return nil
    }

    guard let object = json as? [String: Any] else {
        logMalformedOnce(line: line, reason: "expected a JSON object")
        return nil
    }
    guard let op = stringValue(object["op"]) else {
        logMalformedOnce(line: line, reason: "missing string op")
        return nil
    }

    switch op {
    case "ring":
        let coordinateLimit = 10_000_000.0
        guard let rawBox = object["box"] as? [Any], rawBox.count == 4,
              let x = numberValue(rawBox[0]),
              let y = numberValue(rawBox[1]),
              let width = numberValue(rawBox[2]),
              let height = numberValue(rawBox[3]),
              width > 0.0, height > 0.0,
              abs(x) <= coordinateLimit,
              abs(y) <= coordinateLimit,
              width <= coordinateLimit,
              height <= coordinateLimit else {
            logMalformedOnce(line: line, reason: "ring has an invalid box")
            return nil
        }

        let step: Int
        if object.keys.contains("step") {
            guard let parsedStep = intValue(object["step"]) else {
                logMalformedOnce(line: line, reason: "ring has an invalid step")
                return nil
            }
            step = parsedStep
        } else {
            step = 0
        }

        let total: Int
        if object.keys.contains("total") {
            guard let parsedTotal = intValue(object["total"]) else {
                logMalformedOnce(line: line, reason: "ring has an invalid total")
                return nil
            }
            total = parsedTotal
        } else {
            total = 0
        }

        // label and say are optional; null and absent both mean "not set".
        let labelField = optionalStringField(object, key: "label")
        guard labelField.valid else {
            logMalformedOnce(line: line, reason: "ring has an invalid label")
            return nil
        }
        let sayField = optionalStringField(object, key: "say")
        guard sayField.valid else {
            logMalformedOnce(line: line, reason: "ring has an invalid say")
            return nil
        }

        let spec = RingSpec(box: CGRect(x: x, y: y, width: width, height: height),
                            label: labelField.value,
                            say: sayField.value,
                            step: step,
                            total: total)
        return .ring(spec)

    case "chip":
        guard let step = intValue(object["step"]) else {
            logMalformedOnce(line: line, reason: "chip has an invalid step")
            return nil
        }
        return .chip(step)

    case "pet":
        guard let framesDir = stringValue(object["frames_dir"]), !framesDir.isEmpty else {
            logMalformedOnce(line: line, reason: "pet has an invalid frames_dir")
            return nil
        }
        var scale: CGFloat = 0.33
        if object.keys.contains("scale") {
            guard let rawScale = numberValue(object["scale"]), rawScale > 0.0 else {
                logMalformedOnce(line: line, reason: "pet has an invalid scale")
                return nil
            }
            // Keep a hostile payload from allocating an absurd window.
            scale = CGFloat(min(4.0, max(0.1, rawScale)))
        }
        var enabled = true
        if let rawEnabled = object["pet_enabled"], !(rawEnabled is NSNull) {
            guard let parsedEnabled = rawEnabled as? Bool else {
                logMalformedOnce(line: line,
                                 reason: "pet has an invalid pet_enabled")
                return nil
            }
            enabled = parsedEnabled
        }
        return .pet(PetSpec(framesDir: framesDir, scale: scale, enabled: enabled))

    case "launcher":
        return .launcher

    case "done":
        return .done

    case "axtrusted":
        var prompt = false
        if let rawPrompt = object["prompt"], !(rawPrompt is NSNull) {
            guard let parsedPrompt = rawPrompt as? Bool else {
                logMalformedOnce(line: line, reason: "axtrusted has an invalid prompt")
                return nil
            }
            prompt = parsedPrompt
        }
        return .axtrusted(prompt: prompt)

    case "axcapture":
        let target: String?
        if object.keys.contains("target") {
            if object["target"] is NSNull {
                target = nil
            } else if let text = stringValue(object["target"]) {
                target = text
            } else {
                logMalformedOnce(line: line, reason: "axcapture has an invalid target")
                return nil
            }
        } else {
            target = nil
        }

        let maxElements: Int
        if object.keys.contains("max_elements") {
            guard let parsed = intValue(object["max_elements"]) else {
                logMalformedOnce(line: line, reason: "axcapture has an invalid max_elements")
                return nil
            }
            maxElements = parsed
        } else {
            maxElements = axDefaultMaxElements
        }

        let depth: Int
        if object.keys.contains("depth") {
            guard let parsed = intValue(object["depth"]) else {
                logMalformedOnce(line: line, reason: "axcapture has an invalid depth")
                return nil
            }
            depth = parsed
        } else {
            depth = axDefaultDepth
        }

        return .axcapture(AXCaptureSpec(target: target,
                                        maxElements: maxElements,
                                        depth: depth))

    case "idle":
        return .idle

    case "quit":
        return .quit

    default:
        logMalformedOnce(line: line, reason: "unknown op \"\(op)\"")
        return nil
    }
}

// MARK: - Ring layout

private struct RingLayout {
    let ringRect: CGRect
    let chipRect: CGRect
    let bubbleRect: CGRect?
    let canvasRect: CGRect
    let stepText: String
    let sayText: String
    let chipFont: NSFont
    let bubbleFont: NSFont
    let bubbleStyle: NSParagraphStyle

    init(spec: RingSpec) {
        let ring = spec.box.insetBy(dx: -8.0, dy: -8.0)

        let chipFont = NSFont.monospacedSystemFont(ofSize: 10.0, weight: .medium)
        let stepText = "step \(spec.step)/\(spec.total)"
        let chipText = textSize(stepText,
                                font: chipFont,
                                maxWidth: CGFloat.greatestFiniteMagnitude,
                                paragraphStyle: nil)
        let chipWidth = max(44.0, chipText.width + 12.0)
        let chipHeight = chipText.height + 6.0
        let chip = CGRect(x: ring.minX,
                          y: ring.minY - chipHeight - 6.0,
                          width: chipWidth,
                          height: chipHeight)

        let bubbleFont = NSFont.systemFont(ofSize: 11.0, weight: .regular)
        let bubbleStyle = makeParagraphStyle(lineBreakMode: .byWordWrapping)
        var bubble: CGRect?
        let say = spec.say ?? ""

        var union = ring.union(chip)
        if !say.isEmpty {
            // 10pt padding per side: a 400pt text column keeps the outer
            // bubble at or below the hard 420pt width limit. Height is taken
            // from the wrapped measurement, so every additional line grows
            // the bubble rather than overflowing it.
            let textMaxWidth: CGFloat = 400.0
            let maxBubbleWidth: CGFloat = 420.0
            let textBounds = textSize(say,
                                      font: bubbleFont,
                                      maxWidth: textMaxWidth,
                                      paragraphStyle: bubbleStyle)
            let bubbleWidth = min(maxBubbleWidth, textBounds.width + 20.0)
            let bubbleHeight = textBounds.height + 16.0
            var bubbleRect = CGRect(x: ring.minX,
                                    y: ring.maxY + 10.0,
                                    width: max(44.0, bubbleWidth),
                                    height: bubbleHeight)

            // The chip sits above the ring and the bubble below it, so these
            // should already be disjoint. Keep an explicit guard so a future
            // layout change can never paint the bubble over the step chip.
            if bubbleRect.intersects(chip) {
                bubbleRect.origin.y = chip.maxY + 10.0
            }

            bubble = bubbleRect
            union = union.union(bubbleRect)
        }

        self.ringRect = ring
        self.chipRect = chip
        self.bubbleRect = bubble
        self.canvasRect = union.insetBy(dx: -16.0, dy: -16.0)
        self.stepText = stepText
        self.sayText = say
        self.chipFont = chipFont
        self.bubbleFont = bubbleFont
        self.bubbleStyle = bubbleStyle
    }
}

// MARK: - Ring view

private final class RingView: NSView {
    private let layout: RingLayout
    private let contentOrigin: CGPoint

    init(frame: NSRect, layout: RingLayout, contentOrigin: CGPoint) {
        self.layout = layout
        self.contentOrigin = contentOrigin
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var isFlipped: Bool {
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        let ringLocal = layout.ringRect.offsetBy(dx: -contentOrigin.x,
                                                 dy: -contentOrigin.y)
        let chipLocal = layout.chipRect.offsetBy(dx: -contentOrigin.x,
                                                 dy: -contentOrigin.y)

        // (a) Rounded-rect ring around the target, expanded 8pt from the box.
        let ringPath = NSBezierPath(roundedRect: ringLocal, xRadius: 10.0, yRadius: 10.0)
        ringPath.lineWidth = 3.0
        NSColor(red: 122.0 / 255.0,
                green: 134.0 / 255.0,
                blue: 255.0 / 255.0,
                alpha: 0.9).setStroke()
        ringPath.stroke()

        // (b) Step chip above-left of the ring.
        let chipPath = NSBezierPath(roundedRect: chipLocal, xRadius: 5.0, yRadius: 5.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 26.0 / 255.0,
                alpha: 1.0).setFill()
        chipPath.fill()

        let chipTextRect = chipLocal.insetBy(dx: 6.0, dy: 3.0)
        let chipAttributes: [NSAttributedString.Key: Any] = [
            .font: layout.chipFont,
            .foregroundColor: NSColor.white
        ]
        (layout.stepText as NSString).draw(in: chipTextRect,
                                           withAttributes: chipAttributes)

        // (c) Speech bubble below the ring, max width 420pt.
        if let bubble = layout.bubbleRect {
            let bubbleLocal = bubble.offsetBy(dx: -contentOrigin.x,
                                              dy: -contentOrigin.y)
            let bubblePath = NSBezierPath(roundedRect: bubbleLocal,
                                          xRadius: 8.0,
                                          yRadius: 8.0)
            NSColor(red: 22.0 / 255.0,
                    green: 23.0 / 255.0,
                    blue: 26.0 / 255.0,
                    alpha: 0.82).setFill()
            bubblePath.fill()
            NSColor(white: 1.0, alpha: 0.10).setStroke()
            bubblePath.lineWidth = 1.0
            bubblePath.stroke()

            let textRect = bubbleLocal.insetBy(dx: 10.0, dy: 8.0)
            let bubbleAttributes: [NSAttributedString.Key: Any] = [
                .font: layout.bubbleFont,
                .foregroundColor: NSColor.white,
                .paragraphStyle: layout.bubbleStyle
            ]
            let attributed = NSAttributedString(string: layout.sayText,
                                                attributes: bubbleAttributes)
            attributed.draw(with: textRect,
                            options: [.usesLineFragmentOrigin, .usesFontLeading],
                            context: nil)
        }
    }
}

// MARK: - Chip view

private final class ChipView: NSView {
    private let text: String
    private let font: NSFont

    init(frame: NSRect, text: String, font: NSFont) {
        self.text = text
        self.font = font
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var isFlipped: Bool {
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        let background = bounds.insetBy(dx: 1.0, dy: 1.0)
        let path = NSBezierPath(roundedRect: background,
                                xRadius: background.height / 2.0,
                                yRadius: background.height / 2.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 26.0 / 255.0,
                alpha: 0.88).setFill()
        path.fill()

        let style = NSMutableParagraphStyle()
        style.alignment = .center
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.white,
            .paragraphStyle: style
        ]
        let textHeight = (text as NSString).size(withAttributes: attributes).height
        let textRect = NSRect(x: 10.0,
                              y: (bounds.height - textHeight) / 2.0,
                              width: max(0.0, bounds.width - 20.0),
                              height: textHeight)
        (text as NSString).draw(in: textRect, withAttributes: attributes)
    }
}

// MARK: - Pet sprite sheet

/// Decoded spritesheet plus the slicing metadata the real pet manifests do not
/// carry. Sheets follow the desktop app convention: 192pt-wide cells, 208pt
/// rows, and rows in DEFAULT_STATE_ROWS order. Some generated sheets append
/// extra gaze rows; those are sliced but never played by this overlay.
private struct PetSheet {
    let image: NSImage
    let cellWidth: CGFloat
    let cellHeight: CGFloat
    let columns: Int
    let rows: Int
    let frameCounts: [Int]
    private let rowIndexes: [String: Int]

    static let defaultFrameWidth = 192.0
    static let defaultFrameHeight = 208.0
    static let defaultFrameCount = 6

    // Mirrors DEFAULT_STATE_ROWS from the desktop pet-sprite component.
    static let currentStateRows = [
        "idle",
        "running-right",
        "running-left",
        "waving",
        "jumping",
        "failed",
        "waiting",
        "running",
        "review"
    ]

    // Older seven-row sheets predate the directional running rows.
    static let legacyStateRows = [
        "idle",
        "waving",
        "jumping",
        "failed",
        "waiting",
        "running",
        "review"
    ]

    static func load(framesDir: String) -> PetSheet? {
        let manager = FileManager.default
        let manifestPath = (framesDir as NSString).appendingPathComponent("pet.json")

        var sheetPath = (framesDir as NSString).appendingPathComponent("spritesheet.webp")
        if let data = manager.contents(atPath: manifestPath),
           let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let relative = object["spritesheetPath"] as? String,
           !relative.isEmpty {
            let candidate = relative.hasPrefix("/")
                ? relative
                : (framesDir as NSString).appendingPathComponent(relative)
            if manager.fileExists(atPath: candidate) {
                sheetPath = candidate
            }
        }

        guard manager.fileExists(atPath: sheetPath),
              let source = NSImage(contentsOfFile: sheetPath),
              let cgImage = source.cgImage(forProposedRect: nil,
                                           context: nil,
                                           hints: nil),
              cgImage.width > 0, cgImage.height > 0 else {
            return nil
        }

        let pixelWidth = cgImage.width
        let pixelHeight = cgImage.height

        let columns = max(1, Int((Double(pixelWidth) / defaultFrameWidth).rounded()))
        let rowGuess = max(1, Int((Double(pixelHeight) / defaultFrameHeight).rounded()))
        let cellHeightPx = max(1, pixelHeight / rowGuess)
        let cellWidthPx = max(1, pixelWidth / columns)
        let rows = max(1, pixelHeight / cellHeightPx)

        let names: [String]
        if rows == legacyStateRows.count {
            names = legacyStateRows
        } else if rows >= currentStateRows.count {
            names = currentStateRows + (currentStateRows.count..<rows).map { "extra-\($0)" }
        } else if rows < legacyStateRows.count {
            names = Array(legacyStateRows.prefix(rows))
        } else {
            names = Array(currentStateRows.prefix(rows))
        }

        // pet.json in the wild has no framesByRow metadata. Detect the real
        // frame count per row from the sheet's alpha instead of drawing the
        // blank padding cells every uneven row has after its last frame.
        let bytesPerRow = pixelWidth * 4
        var pixels = [UInt8](repeating: 0, count: bytesPerRow * pixelHeight)
        var decoded = false
        let bitmapInfo = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue)
        pixels.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress,
                  let context = CGContext(data: base,
                                          width: pixelWidth,
                                          height: pixelHeight,
                                          bitsPerComponent: 8,
                                          bytesPerRow: bytesPerRow,
                                          space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: bitmapInfo.rawValue) else {
                return
            }
            context.draw(cgImage,
                         in: CGRect(x: 0, y: 0, width: pixelWidth, height: pixelHeight))
            decoded = true
        }
        guard decoded else {
            return nil
        }

        var frameCounts = [Int](repeating: 0, count: rows)
        for row in 0..<rows {
            var lastOccupied = -1
            let yStart = row * cellHeightPx
            let yEnd = min(pixelHeight, yStart + cellHeightPx)
            for column in 0..<columns {
                let xStart = column * cellWidthPx
                let xEnd = min(pixelWidth, xStart + cellWidthPx)
                var occupied = false
                scan: for y in yStart..<yEnd {
                    let rowBase = y * bytesPerRow
                    for x in xStart..<xEnd where pixels[rowBase + x * 4 + 3] > 8 {
                        occupied = true
                        break scan
                    }
                }
                if occupied {
                    lastOccupied = column
                }
            }
            frameCounts[row] = lastOccupied >= 0 ? lastOccupied + 1 : 0
        }

        let idleRow = names.firstIndex(of: "idle") ?? 0
        let fallbackCount = frameCounts[idleRow] > 0
            ? frameCounts[idleRow]
            : max(1, min(columns, defaultFrameCount))
        for row in 0..<frameCounts.count where frameCounts[row] <= 0 {
            frameCounts[row] = fallbackCount
        }

        var rowIndexes: [String: Int] = [:]
        for (index, name) in names.enumerated() {
            rowIndexes[name] = index
        }

        // Normalize the image to pixel-sized points so the source rect used by
        // NSImage.draw is expressed in the same space as the slicing math.
        let normalized = NSImage(cgImage: cgImage,
                                 size: NSSize(width: pixelWidth, height: pixelHeight))

        return PetSheet(image: normalized,
                        cellWidth: CGFloat(cellWidthPx),
                        cellHeight: CGFloat(cellHeightPx),
                        columns: columns,
                        rows: rows,
                        frameCounts: frameCounts,
                        rowIndexes: rowIndexes)
    }

    func rowIndex(_ name: String) -> Int? {
        return rowIndexes[name]
    }

    func frameCount(for row: Int) -> Int {
        guard row >= 0, row < frameCounts.count else {
            return 1
        }
        return max(1, frameCounts[row])
    }
}

// MARK: - Pet view

private final class PetView: NSView {
    private let sheet: PetSheet
    var row: Int = 0
    var frameIndex: Int = 0
    var mirrored: Bool = false
    var spriteOpacity: CGFloat = 0.75

    init(frame: NSRect, sheet: PetSheet) {
        self.sheet = sheet
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var isFlipped: Bool {
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        guard bounds.width > 0.0, bounds.height > 0.0 else {
            return
        }
        let source = NSRect(x: CGFloat(frameIndex) * sheet.cellWidth,
                            y: CGFloat(sheet.rows - 1 - row) * sheet.cellHeight,
                            width: sheet.cellWidth,
                            height: sheet.cellHeight)

        if mirrored {
            NSGraphicsContext.saveGraphicsState()
            let transform = NSAffineTransform()
            transform.translateX(by: bounds.width, yBy: 0.0)
            transform.scaleX(by: -1.0, yBy: 1.0)
            transform.concat()
        }

        sheet.image.draw(in: bounds,
                         from: source,
                         operation: .sourceOver,
                         fraction: max(0.0, min(1.0, spriteOpacity)),
                         respectFlipped: true,
                         hints: [.interpolation: NSImageInterpolation.high.rawValue])

        if mirrored {
            NSGraphicsContext.restoreGraphicsState()
        }
    }
}

// MARK: - Pet launcher nameplate

/// The small "butler" chip above the double. It is its own borderless window so
/// the sprite window can stay click-through: only this chip ever accepts clicks.
private final class NameplateView: NSView {
    private let text: String
    private let font: NSFont
    var onClick: (() -> Void)?

    init(frame: NSRect, text: String, font: NSFont) {
        self.text = text
        self.font = font
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var isFlipped: Bool {
        return true
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    override func mouseDown(with event: NSEvent) {
        onClick?()
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        let background = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: background, xRadius: 5.0, yRadius: 5.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 26.0 / 255.0,
                alpha: 0.9).setFill()
        path.fill()
        NSColor(white: 1.0, alpha: 0.10).setStroke()
        path.lineWidth = 1.0
        path.stroke()

        let style = NSMutableParagraphStyle()
        style.alignment = .center
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.white,
            .paragraphStyle: style
        ]
        let textHeight = (text as NSString).size(withAttributes: attributes).height
        let textRect = NSRect(x: 4.0,
                              y: (bounds.height - textHeight) / 2.0,
                              width: max(0.0, bounds.width - 8.0),
                              height: textHeight)
        (text as NSString).draw(in: textRect, withAttributes: attributes)
    }
}

// MARK: - Overlay controller

private final class OverlayController {
    private var ringWindows: [NSWindow] = []
    private var chipWindow: NSWindow?
    private var stdinThread: Thread?
    private var activeRing: RingSpec?
    private var observerTokens: [(NotificationCenter, NSObjectProtocol)] = []
    private var isShuttingDown = false

    // The pet double is independent of the ring windows: it survives `idle`
    // and only goes away on `quit`/EOF or when a pet directory cannot load.
    private var petWindow: NSWindow?
    private var petView: PetView?
    private var petSheet: PetSheet?
    private var petScale: CGFloat = 0.33
    private var petTimer: Timer?
    private var petCurrentFrame: CGRect?
    private var petTargetFrame: CGRect?
    private var petMoveStartFrame: CGRect?
    private var petMoveStartTime: TimeInterval = 0.0
    private var petMoving = false
    private var petMovingRight = false
    private var petRow = 0
    private var petFrameIndex = 0
    private var petLastFrameAdvance: TimeInterval = 0.0
    private var lastRingBox: CGRect?

    // Moving and parked are distinct alpha states, eased over the same 1.2s as
    // the walk so the double never snaps between them.
    private let petMovingOpacity: CGFloat = 0.55
    private let petParkedOpacity: CGFloat = 0.75
    private let petOpacityDuration: TimeInterval = 1.2
    private var petOpacity: CGFloat = 0.75
    private var petOpacityStart: CGFloat = 0.75
    private var petOpacityTarget: CGFloat = 0.75
    private var petOpacityStartTime: TimeInterval = 0.0

    // The nameplate is a separate, tiny window: the sprite window must stay
    // click-through at all times.
    private var nameplateWindow: NSWindow?
    private var nameplateView: NameplateView?
    private var launcherActive = false

    // "done" is a farewell: wave for ~0.8s, then fade the double (and its
    // nameplate) out over ~1.5s before closing the window.
    private let petWaveDuration: TimeInterval = 0.8
    private let petFadeDuration: TimeInterval = 1.5
    private var petFarewell = false
    private var petFading = false
    private var petFadeStartTime: TimeInterval = 0.0
    private var petFadeFromAlpha: CGFloat = 1.0

    func start() {
        guard stdinThread == nil else {
            return
        }
        observeEnvironment()
        let thread = Thread { [weak self] in
            self?.readStdin()
        }
        thread.name = "butler-overlay-stdin"
        stdinThread = thread
        thread.start()
    }

    private func readStdin() {
        while let line = readLine(strippingNewline: true) {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                continue
            }
            guard let command = parseCommand(line: line) else {
                continue
            }
            DispatchQueue.main.async { [weak self] in
                self?.handle(command)
            }
        }

        // EOF means the host has finished speaking: shut down cleanly.
        DispatchQueue.main.async { [weak self] in
            self?.shutdown()
            NSApplication.shared.terminate(nil)
        }
    }

    private func handle(_ command: OverlayCommand) {
        if isShuttingDown {
            return
        }
        switch command {
        case .ring(let spec):
            showRing(spec)
        case .chip(let step):
            showChip(step: step)
        case .pet(let spec):
            showPet(spec)
        case .launcher:
            showLauncher()
        case .done:
            petDone()
        case .axtrusted(let prompt):
            writeAXResponse(axTrustedResponse(prompt: prompt))
        case .axcapture(let spec):
            writeAXResponse(axCaptureSnapshot(target: spec.target,
                                              maxElements: spec.maxElements,
                                              depth: spec.depth))
        case .idle:
            activeRing = nil
            hideRings(close: true)
            hideChip()
        case .quit:
            shutdown()
            NSApplication.shared.terminate(nil)
        }
    }

    // MARK: Environment

    private func observeEnvironment() {
        let workspaceCenter = NSWorkspace.shared.notificationCenter
        let defaultCenter = NotificationCenter.default

        // canJoinAllSpaces keeps windows on every Space; re-showing on Space
        // changes re-anchors them if the window server moved or hid them.
        let spaceToken = workspaceCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.reanchor()
        }
        observerTokens.append((workspaceCenter, spaceToken))

        let wakeToken = workspaceCenter.addObserver(
            forName: NSWorkspace.screensDidWakeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.reanchor()
        }
        observerTokens.append((workspaceCenter, wakeToken))

        let sleepToken = workspaceCenter.addObserver(
            forName: NSWorkspace.screensDidSleepNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.hideRings(close: true)
            self?.hideChip()
        }
        observerTokens.append((workspaceCenter, sleepToken))

        let screenToken = defaultCenter.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.reanchor()
        }
        observerTokens.append((defaultCenter, screenToken))
    }

    private func reanchor() {
        guard !isShuttingDown, let spec = activeRing else {
            return
        }
        // Screen changes can arrive before NSScreen has settled on the new
        // arrangement; hop once so the rebuild sees the final geometry.
        DispatchQueue.main.async { [weak self] in
            guard let self = self, !self.isShuttingDown else {
                return
            }
            self.showRing(spec)
        }
    }

    // MARK: Ring

    private func showRing(_ spec: RingSpec) {
        guard !isShuttingDown else {
            return
        }
        guard let primaryHeight = primaryScreenHeight(), !NSScreen.screens.isEmpty else {
            logStderr("butler-overlay: ring op dropped: no attached screens")
            return
        }
        guard boxIntersectsAnyScreen(spec.box, primaryHeight: primaryHeight) else {
            logStderr("butler-overlay: ring op dropped: box is off-screen")
            return
        }

        let layout = RingLayout(spec: spec)
        let canvas = layout.canvasRect
        guard canvas.width.isFinite, canvas.height.isFinite,
              canvas.width > 0.0, canvas.height > 0.0 else {
            logStderr("butler-overlay: ring op dropped: empty canvas")
            return
        }

        let slices = visibleSlices(forQuartz: canvas, primaryHeight: primaryHeight)
        guard !slices.isEmpty else {
            logStderr("butler-overlay: ring op dropped: box has no visible screen area")
            return
        }

        hideRings(close: true)
        activeRing = spec
        lastRingBox = spec.box

        for slice in slices {
            let window = NSWindow(contentRect: slice.frame,
                                  styleMask: [.borderless],
                                  backing: .buffered,
                                  defer: false)
            window.isOpaque = false
            window.backgroundColor = .clear
            window.hasShadow = false
            window.level = .statusBar
            window.collectionBehavior = [.canJoinAllSpaces,
                                         .stationary,
                                         .fullScreenAuxiliary,
                                         .ignoresCycle]
            window.ignoresMouseEvents = true
            window.isMovable = false
            window.isMovableByWindowBackground = false
            window.isReleasedWhenClosed = false
            window.acceptsMouseMovedEvents = false
            window.title = spec.label ?? ""

            let content = RingView(frame: NSRect(origin: .zero, size: slice.frame.size),
                                   layout: layout,
                                   contentOrigin: slice.contentOrigin)
            content.autoresizingMask = [.width, .height]
            window.contentView = content

            // orderFrontRegardless presents without activating the app, so the
            // overlay never steals focus or key status from the user's app.
            window.orderFrontRegardless()
            ringWindows.append(window)
        }

        retargetPet(to: spec.box)
    }

    private func hideRings(close: Bool) {
        for window in ringWindows {
            window.orderOut(nil)
            if close {
                window.close()
            }
        }
        if close {
            ringWindows.removeAll()
        }
    }

    // MARK: Chip

    private func showChip(step: Int) {
        hideChip()

        let text = "✓ step \(step)"
        let font = NSFont.systemFont(ofSize: 11.0, weight: .semibold)
        let textBounds = (text as NSString).size(withAttributes: [.font: font])
        let width = ceil(textBounds.width) + 24.0
        let height: CGFloat = 26.0

        guard let screen = primaryScreen() else {
            logStderr("butler-overlay: chip op dropped: no attached screens")
            return
        }
        let visible = screen.visibleFrame
        let frame = NSRect(x: visible.midX - width / 2.0,
                           y: visible.maxY - height - 16.0,
                           width: width,
                           height: height)

        let window = NSWindow(contentRect: frame,
                              styleMask: [.borderless],
                              backing: .buffered,
                              defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.level = .statusBar
        window.collectionBehavior = [.canJoinAllSpaces,
                                     .stationary,
                                     .fullScreenAuxiliary,
                                     .ignoresCycle]
        window.ignoresMouseEvents = true
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false

        let content = ChipView(frame: NSRect(origin: .zero, size: frame.size),
                               text: text,
                               font: font)
        content.autoresizingMask = [.width, .height]
        window.contentView = content
        chipWindow = window

        window.orderFrontRegardless()

        // Keep the window alive only for the requested 2.5s; the identity check
        // makes sure a newer chip is never torn down by an older timer.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self, weak window] in
            guard let self = self, let window = window else {
                return
            }
            if self.chipWindow === window {
                self.chipWindow = nil
                window.orderOut(nil)
                window.close()
            }
        }
    }

    private func hideChip() {
        if let window = chipWindow {
            window.orderOut(nil)
            window.close()
        }
        chipWindow = nil
    }

    // MARK: Pet double

    private func showPet(_ spec: PetSpec) {
        guard !isShuttingDown else {
            return
        }
        guard spec.enabled else {
            // display.pet.enabled is false: keep the ring/chip overlay alive,
            // but never draw a companion double.
            closePetWindow()
            petSheet = nil
            return
        }
        guard let primaryHeight = primaryScreenHeight(), !NSScreen.screens.isEmpty else {
            logPetFailureOnce(framesDir: spec.framesDir, reason: "no attached screens")
            return
        }
        guard let sheet = PetSheet.load(framesDir: spec.framesDir) else {
            // A broken pet must never take the ring/chip overlay down with it.
            closePetWindow()
            petSheet = nil
            logPetFailureOnce(framesDir: spec.framesDir,
                              reason: "spritesheet missing or unreadable")
            return
        }

        closePetWindow()
        petSheet = sheet
        petScale = spec.scale
        petRow = parkedPetRow(for: sheet)
        petFrameIndex = 0
        petLastFrameAdvance = ProcessInfo.processInfo.systemUptime
        petFarewell = false
        petFading = false
        petOpacity = petParkedOpacity
        petOpacityStart = petParkedOpacity
        petOpacityTarget = petParkedOpacity
        petOpacityStartTime = petLastFrameAdvance

        let size = CGSize(width: sheet.cellWidth * petScale,
                          height: sheet.cellHeight * petScale)
        let startFrame: CGRect
        if let box = lastRingBox,
           let target = targetPetFrame(for: box,
                                       size: size,
                                       primaryHeight: primaryHeight) {
            startFrame = target
        } else {
            startFrame = defaultPetFrame(size: size, primaryHeight: primaryHeight)
        }

        petCurrentFrame = startFrame
        petTargetFrame = startFrame
        petMoveStartFrame = nil
        petMoving = false
        createPetWindow(frame: startFrame, primaryHeight: primaryHeight, sheet: sheet)
        createNameplateWindow(for: startFrame, primaryHeight: primaryHeight)
        startPetTimer()
    }

    private func createPetWindow(frame: CGRect,
                                 primaryHeight: CGFloat,
                                 sheet: PetSheet) {
        let appKitFrame = appKitRect(fromQuartz: frame, primaryHeight: primaryHeight)
        let window = NSWindow(contentRect: appKitFrame,
                              styleMask: [.borderless],
                              backing: .buffered,
                              defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.level = .statusBar
        window.collectionBehavior = [.canJoinAllSpaces,
                                     .stationary,
                                     .fullScreenAuxiliary,
                                     .ignoresCycle]
        window.ignoresMouseEvents = true
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false

        let content = PetView(frame: NSRect(origin: .zero, size: appKitFrame.size),
                              sheet: sheet)
        content.row = petRow
        content.frameIndex = petFrameIndex
        content.spriteOpacity = petOpacity
        content.autoresizingMask = [.width, .height]
        window.contentView = content

        petWindow = window
        petView = content
        window.orderFrontRegardless()
    }

    private func closePetWindow() {
        if let window = petWindow {
            window.orderOut(nil)
            window.close()
        }
        petWindow = nil
        petView = nil
        closeNameplate()
    }

    private func closeNameplate() {
        if let window = nameplateWindow {
            window.orderOut(nil)
            window.close()
        }
        nameplateWindow = nil
        nameplateView = nil
    }

    // MARK: Pet nameplate / launcher

    private func nameplateFont() -> NSFont {
        return NSFont.monospacedSystemFont(ofSize: 9.5, weight: .medium)
    }

    private func nameplateHeight() -> CGFloat {
        let textHeight = ("butler" as NSString).size(
            withAttributes: [.font: nameplateFont()]).height
        return ceil(textHeight) + 8.0 // 4pt padding above and below
    }

    private func nameplateFrame(for petFrame: CGRect,
                                primaryHeight: CGFloat) -> CGRect {
        let height = nameplateHeight()
        var frame = CGRect(x: petFrame.minX,
                           y: petFrame.minY - height - 2.0,
                           width: petFrame.width,
                           height: height)
        if let screen = screenContaining(petFrame, primaryHeight: primaryHeight) {
            let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                     primaryHeight: primaryHeight)
            if frame.minY < visible.minY + 2.0 {
                frame.origin.y = visible.minY + 2.0
            }
        }
        return frame
    }

    private func createNameplateWindow(for petFrame: CGRect,
                                       primaryHeight: CGFloat) {
        closeNameplate()

        let quartzFrame = nameplateFrame(for: petFrame, primaryHeight: primaryHeight)
        let appKitFrame = appKitRect(fromQuartz: quartzFrame,
                                     primaryHeight: primaryHeight)
        let window = NSWindow(contentRect: appKitFrame,
                              styleMask: [.borderless],
                              backing: .buffered,
                              defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.level = .statusBar
        window.collectionBehavior = [.canJoinAllSpaces,
                                     .stationary,
                                     .fullScreenAuxiliary,
                                     .ignoresCycle]
        // Only this tiny chip window is ever clickable; the sprite window
        // remains ignoresMouseEvents = true.
        window.ignoresMouseEvents = !launcherActive
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false

        let content = NameplateView(frame: NSRect(origin: .zero,
                                                  size: appKitFrame.size),
                                    text: "butler",
                                    font: nameplateFont())
        content.onClick = { [weak self] in
            self?.writeLauncherClick()
        }
        content.autoresizingMask = [.width, .height]
        window.contentView = content

        nameplateWindow = window
        nameplateView = content
        window.orderFrontRegardless()
    }

    private func updateNameplateFrame(for petFrame: CGRect,
                                      primaryHeight: CGFloat) {
        guard let window = nameplateWindow else {
            return
        }
        let quartzFrame = nameplateFrame(for: petFrame, primaryHeight: primaryHeight)
        window.setFrame(appKitRect(fromQuartz: quartzFrame,
                                   primaryHeight: primaryHeight),
                        display: true)
    }

    private func showLauncher() {
        launcherActive = true
        if let window = nameplateWindow {
            window.ignoresMouseEvents = false
            window.orderFrontRegardless()
        }
    }

    private func writeLauncherClick() {
        guard launcherActive else {
            return
        }
        let payload = "{\"op\":\"launcher_click\"}\n"
        FileHandle.standardOutput.write(Data(payload.utf8))
    }

    // MARK: Pet farewell

    private func petDone() {
        guard !isShuttingDown, petWindow != nil, petSheet != nil else {
            return
        }
        petMoving = false
        petFarewell = true
        petFading = false

        let now = ProcessInfo.processInfo.systemUptime
        if let sheet = petSheet,
           let wave = sheet.rowIndex("waving") ?? sheet.rowIndex("jumping") {
            petRow = wave
            petFrameIndex = 0
            petLastFrameAdvance = now
            petView?.row = wave
            petView?.frameIndex = 0
            petView?.needsDisplay = true
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + petWaveDuration) { [weak self] in
            self?.startPetFadeOut()
        }
    }

    private func startPetFadeOut() {
        guard !isShuttingDown, petWindow != nil, !petFading else {
            return
        }
        petFading = true
        petFadeStartTime = ProcessInfo.processInfo.systemUptime
        petFadeFromAlpha = petWindow?.alphaValue ?? 1.0
    }

    private func finishPetFarewell() {
        petFarewell = false
        petFading = false
        petTimer?.invalidate()
        petTimer = nil
        petCurrentFrame = nil
        petTargetFrame = nil
        petMoveStartFrame = nil
        petMoving = false
        closePetWindow()
        petSheet = nil
    }

    private func startPetTimer() {
        guard petTimer == nil else {
            return
        }
        let timer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.tickPet()
        }
        RunLoop.main.add(timer, forMode: .common)
        petTimer = timer
    }

    /// Ease the double toward the ring's trailing (right) edge over ~1.2s.
    private func retargetPet(to box: CGRect) {
        guard !isShuttingDown,
              let sheet = petSheet,
              petWindow != nil,
              let primaryHeight = primaryScreenHeight(),
              let current = petCurrentFrame else {
            return
        }

        let size = CGSize(width: sheet.cellWidth * petScale,
                          height: sheet.cellHeight * petScale)
        guard let target = targetPetFrame(for: box,
                                          size: size,
                                          primaryHeight: primaryHeight) else {
            return
        }

        petTargetFrame = target
        let sameSpot = abs(current.minX - target.minX) < 0.5
            && abs(current.minY - target.minY) < 0.5
        if sameSpot {
            petMoving = false
            petCurrentFrame = target
            setPetOpacityTarget(petParkedOpacity)
            updateNameplateFrame(for: target, primaryHeight: primaryHeight)
            return
        }

        petMoveStartFrame = current
        petMoveStartTime = ProcessInfo.processInfo.systemUptime
        petMoving = true
        setPetOpacityTarget(petMovingOpacity)

        let dx = target.midX - current.midX
        if abs(dx) > 0.5 {
            petMovingRight = dx > 0.0
        }
    }

    private func setPetOpacityTarget(_ target: CGFloat) {
        if abs(petOpacityTarget - target) < 0.001 {
            return
        }
        petOpacityStart = petOpacity
        petOpacityTarget = target
        petOpacityStartTime = ProcessInfo.processInfo.systemUptime
    }

    private func tickPet() {
        guard !isShuttingDown,
              let sheet = petSheet,
              let window = petWindow,
              let view = petView,
              let primaryHeight = primaryScreenHeight() else {
            return
        }

        let now = ProcessInfo.processInfo.systemUptime

        if petFading {
            let progress = min(1.0, max(0.0,
                (now - petFadeStartTime) / petFadeDuration))
            // Smoothstep the fade as well: no visible snap at either end.
            let eased = progress * progress * (3.0 - 2.0 * progress)
            let alpha = petFadeFromAlpha * (1.0 - eased)
            window.alphaValue = alpha
            nameplateWindow?.alphaValue = alpha
            if progress >= 1.0 {
                finishPetFarewell()
                return
            }
        }

        if petMoving,
           let start = petMoveStartFrame,
           let target = petTargetFrame {
            let duration = 1.2
            let progress = min(1.0, max(0.0, (now - petMoveStartTime) / duration))
            // Smoothstep: zero velocity at both ends, so it never teleports.
            let eased = progress * progress * (3.0 - 2.0 * progress)
            let frame = CGRect(x: start.minX + (target.minX - start.minX) * eased,
                               y: start.minY + (target.minY - start.minY) * eased,
                               width: target.width,
                               height: target.height)
            petCurrentFrame = frame
            window.setFrame(appKitRect(fromQuartz: frame, primaryHeight: primaryHeight),
                            display: true)
            updateNameplateFrame(for: frame, primaryHeight: primaryHeight)
            if progress >= 1.0 {
                petMoving = false
                petCurrentFrame = target
                setPetOpacityTarget(petParkedOpacity)
                updateNameplateFrame(for: target, primaryHeight: primaryHeight)
            }
        }

        // Ease the sprite alpha (0.55 moving / 0.75 parked) over the same
        // 1.2s smoothstep window as the walk.
        if abs(petOpacity - petOpacityTarget) > 0.001 {
            let progress = min(1.0, max(0.0,
                (now - petOpacityStartTime) / petOpacityDuration))
            let eased = progress * progress * (3.0 - 2.0 * progress)
            petOpacity = petOpacityStart
                + (petOpacityTarget - petOpacityStart) * eased
            if progress >= 1.0 {
                petOpacity = petOpacityTarget
            }
            view.spriteOpacity = petOpacity
            view.needsDisplay = true
        }

        if petFarewell {
            // Hold the wave row through the farewell; no parked-row switch.
            if let wave = sheet.rowIndex("waving") ?? sheet.rowIndex("jumping") {
                if petRow != wave {
                    petRow = wave
                    petFrameIndex = 0
                    petLastFrameAdvance = now
                    view.row = wave
                    view.frameIndex = 0
                    view.needsDisplay = true
                }
            }
            if view.mirrored {
                view.mirrored = false
                view.needsDisplay = true
            }
        } else {
            let animation = petAnimation(for: sheet,
                                         moving: petMoving,
                                         movingRight: petMovingRight)
            if animation.row != petRow {
                petRow = animation.row
                petFrameIndex = 0
                petLastFrameAdvance = now
                view.row = petRow
                view.frameIndex = 0
                view.needsDisplay = true
            }
            if view.mirrored != animation.mirrored {
                view.mirrored = animation.mirrored
                view.needsDisplay = true
            }
        }

        // Roughly 8 fps, matching the desktop sprite's relaxed cadence.
        if now - petLastFrameAdvance >= 0.125 {
            petLastFrameAdvance = now
            let count = sheet.frameCount(for: petRow)
            petFrameIndex = (petFrameIndex + 1) % count
            view.frameIndex = petFrameIndex
            view.needsDisplay = true
        }
    }

    private func petAnimation(for sheet: PetSheet,
                              moving: Bool,
                              movingRight: Bool) -> (row: Int, mirrored: Bool) {
        if moving {
            let left = sheet.rowIndex("running-left")
            let right = sheet.rowIndex("running-right")
            if movingRight {
                if let row = right {
                    return (row, false)
                }
                if let row = left {
                    return (row, true)
                }
            } else {
                if let row = left {
                    return (row, false)
                }
                if let row = right {
                    return (row, true)
                }
            }
            // The undirected running row faces left by convention, so rightward
            // travel is mirrored (same rule as the desktop pet renderer).
            if let row = sheet.rowIndex("running") {
                return (row, movingRight)
            }
            if let row = sheet.rowIndex("run") {
                return (row, movingRight)
            }
        }

        return (parkedPetRow(for: sheet), false)
    }

    private func parkedPetRow(for sheet: PetSheet) -> Int {
        if let row = sheet.rowIndex("waiting") {
            return row
        }
        return sheet.rowIndex("idle") ?? 0
    }

    private func screenContaining(_ box: CGRect,
                                  primaryHeight: CGFloat) -> NSScreen? {
        var bestScreen: NSScreen?
        var bestArea: CGFloat = 0.0
        for screen in NSScreen.screens {
            let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                     primaryHeight: primaryHeight)
            let clipped = box.intersection(visible)
            guard !clipped.isNull, clipped.width > 0.0, clipped.height > 0.0 else {
                continue
            }
            let area = clipped.width * clipped.height
            if area > bestArea {
                bestArea = area
                bestScreen = screen
            }
        }
        return bestScreen ?? primaryScreen()
    }

    private func targetPetFrame(for box: CGRect,
                                size: CGSize,
                                primaryHeight: CGFloat) -> CGRect? {
        guard size.width > 0.0, size.height > 0.0,
              let screen = screenContaining(box, primaryHeight: primaryHeight) else {
            return nil
        }
        let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                 primaryHeight: primaryHeight)

        // Just outside the trailing edge, vertically centered on the control,
        // but always fully on screen.
        var origin = CGPoint(x: box.maxX + 8.0,
                             y: box.midY - size.height / 2.0)
        if visible.width > size.width {
            origin.x = min(max(origin.x, visible.minX + 4.0),
                           visible.maxX - size.width - 4.0)
        } else {
            origin.x = visible.midX - size.width / 2.0
        }
        if visible.height > size.height {
            origin.y = min(max(origin.y, visible.minY + 4.0),
                           visible.maxY - size.height - 4.0)
        } else {
            origin.y = visible.midY - size.height / 2.0
        }
        return CGRect(origin: origin, size: size)
    }

    private func defaultPetFrame(size: CGSize, primaryHeight: CGFloat) -> CGRect {
        guard let screen = primaryScreen() else {
            return CGRect(origin: .zero, size: size)
        }
        let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                 primaryHeight: primaryHeight)
        let origin = CGPoint(x: visible.maxX - size.width - 24.0,
                             y: visible.maxY - size.height - 24.0)
        return CGRect(origin: origin, size: size)
    }

    // MARK: Teardown

    /// Idempotent cleanup used by quit, EOF, and application termination.
    func shutdown() {
        guard !isShuttingDown else {
            return
        }
        isShuttingDown = true
        activeRing = nil
        hideRings(close: true)
        hideChip()
        petTimer?.invalidate()
        petTimer = nil
        closePetWindow()
        petSheet = nil

        for (center, token) in observerTokens {
            center.removeObserver(token)
        }
        observerTokens.removeAll()
        stdinThread = nil
    }
}

// MARK: - Application delegate

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: OverlayController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let controller = OverlayController()
        self.controller = controller
        controller.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // The overlay outlives its windows; stdin decides when to quit.
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        controller?.shutdown()
    }
}

// MARK: - Entry point

private var retainedDelegate: AppDelegate?
private var retainedSignalSources: [DispatchSourceSignal] = []

private func installSignalHandlers() {
    // Catch SIGINT/SIGTERM in the run loop so windows are ordered out before
    // the process exits instead of being abandoned mid-draw.
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)

    for signalNumber in [SIGINT, SIGTERM] {
        let source = DispatchSource.makeSignalSource(signal: signalNumber,
                                                     queue: .main)
        source.setEventHandler {
            NSApplication.shared.terminate(nil)
        }
        source.resume()
        retainedSignalSources.append(source)
    }
}

private func runOverlay() {
    let application = NSApplication.shared
    _ = application.setActivationPolicy(.accessory)

    installSignalHandlers()

    let delegate = AppDelegate()
    retainedDelegate = delegate
    application.delegate = delegate

    application.run()
}

runOverlay()
