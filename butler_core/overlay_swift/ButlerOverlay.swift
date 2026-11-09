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
//    {"op":"claim_chip","enabled":true,"sprite_w":63,"sprite_h":69,"pad_bottom":24}
//    {"op":"launcher"}
//    {"op":"done"}
//    {"op":"axtrusted","prompt":true}
//    {"op":"axcapture","target":null,"max_elements":150,"depth":10}
//    {"op":"idle"}
//    {"op":"quit"}
//
//  Event lines the renderer writes to stdout (never a request response):
//    {"op":"launcher_click"}
//    {"op":"summon_text","text":"...","id":"a1b2c3d4"}
//
//  The legacy {"op":"claim_click"} (text-less) event is no longer emitted by
//  this renderer; the engine still accepts it for older companion binaries.
//
//  Coordinates in "box" are global Accessibility (Quartz) screen coordinates:
//  origin at the top-left of the primary display, y increasing downward.
//
//  The renderer tears down its windows on EOF, "quit", SIGINT, and SIGTERM.
//

import AppKit
import ApplicationServices
import CoreGraphics
import Carbon.HIToolbox

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

/// AX frames for every top-level window of *pid* (Quartz top-left coords).
/// Used by the pop-out claim chip so it anchors to the real pet panel, not a
/// guessed CG shape; an untrusted process returns [] and the caller falls back
/// to the CGWindow bounds.
private func axWindowFrameList(pid: pid_t) -> [(title: String?, frame: CGRect)] {
    let root = AXUIElementCreateApplication(pid)
    guard let rawWindows = copyAXAttribute(root, "AXWindows") as? [Any] else {
        return []
    }
    var out: [(title: String?, frame: CGRect)] = []
    for rawWindow in rawWindows {
        guard let window = axElement(from: rawWindow) else {
            continue
        }
        let fields = readAXFields(window)
        guard fields.box.count == 4,
              let x = numberValue(fields.box[0]),
              let y = numberValue(fields.box[1]),
              let width = numberValue(fields.box[2]),
              let height = numberValue(fields.box[3]),
              width > 0.0, height > 0.0 else {
            continue
        }
        out.append((axWindowTitle(window),
                    CGRect(x: x, y: y, width: width, height: height)))
    }
    return out
}

/// Every top-level window element of the app, as AXUIElement handles. The
/// observer attaches per-window notifications to these; unlike
/// `axWindowFrameList` it keeps elements, not frames.
private func axWindowElements(of root: AXUIElement) -> [AXUIElement] {
    guard let rawWindows = copyAXAttribute(root, "AXWindows") as? [Any] else {
        return []
    }
    return rawWindows.compactMap { axElement(from: $0) }
}

/// AX frame of one window element, read straight off it. This is the
/// notification hot path during a drag: two attribute reads, no AXWindows
/// enumeration and no `CGWindowListCopyWindowInfo` call.
private func axWindowFrame(of element: AXUIElement) -> CGRect? {
    guard let point = axPointOrSize(copyAXAttribute(element, "AXPosition"),
                                    kind: "point"),
          let size = axPointOrSize(copyAXAttribute(element, "AXSize"),
                                   kind: "size"),
          point.count == 2, size.count == 2 else {
        return nil
    }
    let width = CGFloat(size[0])
    let height = CGFloat(size[1])
    guard width > 0.0, height > 0.0 else {
        return nil
    }
    return CGRect(x: CGFloat(point[0]), y: CGFloat(point[1]),
                  width: width, height: height)
}

/// The pop-out pet panel is 240x300 and its send-message bubble is smaller;
/// both pass this filter. The main window (1280x803) and the 248x19 strip do
/// not. Shared by the polled anchor and the per-notification fast path.
private func isPetShapedFrame(_ bounds: CGRect) -> Bool {
    guard bounds.width >= 60.0, bounds.height >= 60.0,
          bounds.width <= 480.0, bounds.height <= 480.0 else {
        return false
    }
    let aspect = bounds.width / max(bounds.height, 1.0)
    return aspect >= 0.6 && aspect <= 1.67
}

private func hermesPid() -> pid_t? {
    if let app = NSRunningApplication
        .runningApplications(withBundleIdentifier: "com.nousresearch.hermes")
        .first {
        return app.processIdentifier
    }
    return NSWorkspace.shared.runningApplications.first {
        !$0.isTerminated && ($0.localizedName ?? "").lowercased().contains("hermes")
    }?.processIdentifier
}

private struct AXRunningWindow {
    let pid: pid_t
    let name: String
    let title: String?
    let bounds: CGRect?
}

private func windowBounds(_ window: [String: Any]) -> CGRect? {
    guard let raw = window[kCGWindowBounds as String] as? [String: Any],
          let x = numberValue(raw["X"]),
          let y = numberValue(raw["Y"]),
          let width = numberValue(raw["Width"]),
          let height = numberValue(raw["Height"]),
          width > 0.0, height > 0.0 else {
        return nil
    }
    return CGRect(x: x, y: y, width: width, height: height)
}

/// CGWindow bounds for every on-screen window owned by *pid*, across all
/// window layers. The pet pop-out is an NSPanel that lives on layer 3, so the
/// layer-0 filter in `axTopmostLayer0Windows()` can never see it. Owner pid is
/// the only identity used: the window name is empty without Screen Recording
/// permission, and owner-name matching is fragile. Pooled with the AX frames
/// every tick rather than tried only after AX fails.
private func cgWindowFrameList(pid: pid_t) -> [CGRect] {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let info = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return []
    }
    var out: [CGRect] = []
    for window in info {
        guard let rawPid = intValue(window[kCGWindowOwnerPID as String]),
              rawPid > 0, pid_t(rawPid) == pid,
              let bounds = windowBounds(window) else {
            continue
        }
        out.append(bounds)
    }
    return out
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
            title: window[kCGWindowName as String] as? String,
            bounds: windowBounds(window)))
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

private struct ClaimChipSpec {
    let enabled: Bool
    /// On-screen sprite size the pet pop-out draws, in points. nil (an old
    /// producer, or a malformed field) keeps the window-frame anchor.
    let spriteW: CGFloat?
    let spriteH: CGFloat?
    let padBottom: CGFloat

    /// Mirrors PET_PADDING_BOTTOM in app/pet-overlay/pet-overlay-app.tsx: the
    /// sprite sits 24 pt above the bottom edge of the pop-out window.
    static let defaultPadBottom: CGFloat = 24.0
}

private enum OverlayCommand {
    case ring(RingSpec)
    case chip(Int)
    case pet(PetSpec)
    case claimChip(ClaimChipSpec)
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

    case "claim_chip":
        var enabled = true
        if let rawEnabled = object["enabled"], !(rawEnabled is NSNull) {
            guard let parsedEnabled = rawEnabled as? Bool else {
                logMalformedOnce(line: line, reason: "claim_chip has an invalid enabled")
                return nil
            }
            enabled = parsedEnabled
        }
        // The sprite anchor fields are advisory: an old producer omits them and
        // a hostile/broken one gets the window-frame fallback. Neither may drop
        // the op, or the pet would lose its only summon door.
        var spriteW: CGFloat?
        if let rawSpriteW = object["sprite_w"], !(rawSpriteW is NSNull) {
            if let parsed = numberValue(rawSpriteW), parsed > 0.0, parsed <= 10_000.0 {
                spriteW = CGFloat(parsed)
            } else {
                logMalformedOnce(line: line, reason: "claim_chip has an invalid sprite_w")
            }
        }
        var spriteH: CGFloat?
        if let rawSpriteH = object["sprite_h"], !(rawSpriteH is NSNull) {
            if let parsed = numberValue(rawSpriteH), parsed > 0.0, parsed <= 10_000.0 {
                spriteH = CGFloat(parsed)
            } else {
                logMalformedOnce(line: line, reason: "claim_chip has an invalid sprite_h")
            }
        }
        var padBottom = ClaimChipSpec.defaultPadBottom
        if let rawPadBottom = object["pad_bottom"], !(rawPadBottom is NSNull) {
            if let parsed = numberValue(rawPadBottom), parsed >= 0.0, parsed <= 10_000.0 {
                padBottom = CGFloat(parsed)
            } else {
                logMalformedOnce(line: line, reason: "claim_chip has an invalid pad_bottom")
            }
        }
        return .claimChip(ClaimChipSpec(enabled: enabled,
                                        spriteW: spriteW,
                                        spriteH: spriteH,
                                        padBottom: padBottom))

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

// MARK: - Pop-out pet claim chip

/// Bounded event journal (`chip-events.log` in the plugin data dir).
///
/// Support needs to know how far a click got before anyone blames the summon
/// pipeline: `hover-in` without `click` means the window swallowed the press,
/// `click` without `emit` means the controller path is broken, `emit` with no
/// `state.json` row means the companion → engine hop is at fault.
/// 200 lines, then it rewrites itself.
private func chipJournal(_ event: String) {
    let dir = (NSHomeDirectory() as NSString)
        .appendingPathComponent(".hermes/plugin-data/butler")
    let path = (dir as NSString).appendingPathComponent("chip-events.log")
    let stamp = String(format: "%.3f", Date().timeIntervalSince1970)
    var lines = (try? String(contentsOfFile: path, encoding: .utf8))?
        .split(separator: "\n", omittingEmptySubsequences: true)
        .map(String.init) ?? []
    lines.append("\(stamp) \(event)")
    if lines.count > 200 {
        lines.removeFirst(lines.count - 200)
    }
    let payload = lines.joined(separator: "\n") + "\n"
    try? payload.write(toFile: path, atomically: true, encoding: .utf8)
}

/// A borderless window cannot become key by default, and a window that cannot
/// become key never routes clicks into its content view. This subclass only
/// relaxes key status; it stays non-main so it never steals app activation.
private final class ClaimChipWindow: NSWindow {
    override var canBecomeKey: Bool {
        return true
    }

    override var canBecomeMain: Bool {
        return false
    }
}

/// A separate, clickable mini window that sits at the TOP-RIGHT of the core
/// Hermes pet pop-out. It never overlaps the pet's sprite bounds, so the pet
/// keeps its own drag/pop-in gestures; this chip is the plugin-owned summon
/// door, drawn as a tiny hat badge rather than a wide text pill.
private final class ClaimChipView: NSView {
    /// The whole chip is one emoji: small enough to read as a hat on the pet.
    private static let glyph = "🎩"
    private static let glyphFont = NSFont.systemFont(ofSize: 13.0)
    /// Hover state drives both the pointing-hand cursor and a brighter chip, so
    /// "this is clickable" survives even when AppKit will not swap the cursor
    /// for a background (inactive) app's window.
    private var hovering = false
    private var hoverTracking: NSTrackingArea?
    var onClick: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        // An icon-only view needs a spoken name or it reads as a blank box.
        setAccessibilityElement(true)
        setAccessibilityLabel("Ask butler")
        setAccessibilityRole(.button)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var isFlipped: Bool {
        return true
    }

    /// Borderless chip windows are not key by default; without first-responder
    /// status AppKit never routes mouseDown to this view.
    override var acceptsFirstResponder: Bool {
        return true
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    /// `.activeAlways` is what makes hover work at all here: this window belongs
    /// to a background accessory process, so the default (active-app-only)
    /// tracking would never fire while the user is working in Hermes.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = hoverTracking {
            removeTrackingArea(existing)
        }
        let area = NSTrackingArea(rect: bounds,
                                  options: [.mouseEnteredAndExited,
                                            .cursorUpdate,
                                            .activeAlways,
                                            .inVisibleRect],
                                  owner: self,
                                  userInfo: nil)
        addTrackingArea(area)
        hoverTracking = area
    }

    /// Cursor rects cover the case where the companion IS the active app.
    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }

    override func cursorUpdate(with event: NSEvent) {
        NSCursor.pointingHand.set()
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        NSCursor.pointingHand.set()
        chipJournal("hover-in")
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        NSCursor.arrow.set()
        chipJournal("hover-out")
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        chipJournal("click")
        onClick?()
    }


    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)

        let background = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: background,
                                xRadius: background.height / 2.0,
                                yRadius: background.height / 2.0)
        if hovering {
            NSColor(red: 38.0 / 255.0,
                    green: 41.0 / 255.0,
                    blue: 54.0 / 255.0,
                    alpha: 0.98).setFill()
        } else {
            NSColor(red: 22.0 / 255.0,
                    green: 23.0 / 255.0,
                    blue: 26.0 / 255.0,
                    alpha: 0.92).setFill()
        }
        path.fill()
        NSColor(red: 122.0 / 255.0,
                green: 134.0 / 255.0,
                blue: 255.0 / 255.0,
                alpha: hovering ? 0.95 : 0.55).setStroke()
        path.lineWidth = 1.0
        path.stroke()

        let attributes: [NSAttributedString.Key: Any] = [
            .font: ClaimChipView.glyphFont,
            .foregroundColor: NSColor.white
        ]
        let glyphSize = (ClaimChipView.glyph as NSString)
            .size(withAttributes: attributes)
        let glyphRect = NSRect(x: (bounds.width - glyphSize.width) / 2.0,
                               y: (bounds.height - glyphSize.height) / 2.0,
                               width: glyphSize.width,
                               height: glyphSize.height)
        (ClaimChipView.glyph as NSString).draw(in: glyphRect,
                                              withAttributes: attributes)
    }
}

// MARK: - In-place ask field

/// Borderless window for the local ask field. It must become KEY so the editor
/// receives keystrokes, but never MAIN: the companion is an accessory process
/// and this is a transient panel, not a document window.
private final class SummonFieldWindow: NSWindow, NSWindowDelegate {
    /// Called when the panel loses key focus (click elsewhere, another app, or
    /// a Space switch). The controller closes the field without emitting.
    var onResignKey: (() -> Void)?

    override var canBecomeKey: Bool {
        return true
    }

    override var canBecomeMain: Bool {
        return false
    }

    func windowDidResignKey(_ notification: Notification) {
        onResignKey?()
    }
}

/// The typing box that appears beside the pet. Same dark fill and indigo border
/// as the claim chip, one single-line `NSTextField`, Enter/Return to send and
/// Escape to cancel.
private final class SummonFieldView: NSView, NSTextFieldDelegate {
    static let fieldWidth: CGFloat = 280.0
    static let fieldHeight: CGFloat = 34.0
    private static let inset: CGFloat = 12.0
    private static let textHeight: CGFloat = 22.0

    let field = NSTextField(frame: .zero)
    var onSubmit: ((String) -> Void)?
    var onCancel: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor

        // The surrounding view paints the dark pill; the field itself stays
        // chromeless so the two never draw two different backgrounds.
        field.placeholderString = "Ask the butler…"
        field.font = NSFont.systemFont(ofSize: 13.0)
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.textColor = NSColor.white
        field.usesSingleLineMode = true
        field.lineBreakMode = .byClipping
        field.cell?.wraps = false
        field.cell?.isScrollable = true
        field.delegate = self
        field.setAccessibilityLabel("Ask the butler")
        addSubview(field)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    override var acceptsFirstResponder: Bool {
        return true
    }

    override func layout() {
        super.layout()
        field.frame = NSRect(x: SummonFieldView.inset,
                             y: (bounds.height - SummonFieldView.textHeight) / 2.0,
                             width: max(0.0, bounds.width - SummonFieldView.inset * 2.0),
                             height: SummonFieldView.textHeight)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let background = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: background, xRadius: 8.0, yRadius: 8.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 26.0 / 255.0,
                alpha: 0.98).setFill()
        path.fill()
        NSColor(red: 122.0 / 255.0,
                green: 134.0 / 255.0,
                blue: 255.0 / 255.0,
                alpha: 0.75).setStroke()
        path.lineWidth = 1.0
        path.stroke()
    }

    /// Return/Enter sends, Escape cancels. `doCommandBy` is the only place a
    /// single-line field reports those keys before they beep or end editing.
    func control(_ control: NSControl,
                 textView: NSTextView,
                 doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.insertNewline(_:))
            || commandSelector == #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)) {
            onSubmit?(field.stringValue)
            return true
        }
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
            onCancel?()
            return true
        }
        return false
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
    let loopMs: Double
    private let rowIndexes: [String: Int]

    static let defaultFrameWidth = 192.0
    static let defaultFrameHeight = 208.0
    static let defaultFrameCount = 6
    // Mirrors LOOP_MS in the desktop app: one full loop for a single state.
    static let defaultLoopMs = 1100.0
    static let minLoopMs = 200.0
    static let maxLoopMs = 5000.0

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

        var manifest: [String: Any]?
        if let data = manager.contents(atPath: manifestPath),
           let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            manifest = object
        }

        var loopMs = defaultLoopMs
        if let rawLoopMs = manifest?["loopMs"], let parsedLoopMs = numberValue(rawLoopMs) {
            loopMs = min(maxLoopMs, max(minLoopMs, parsedLoopMs))
        }

        var sheetPath = (framesDir as NSString).appendingPathComponent("spritesheet.webp")
        if let relative = manifest?["spritesheetPath"] as? String,
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
                        loopMs: loopMs,
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

/// C trampoline for the Hermes AX observer. The controller is handed over as
/// the refcon on every `AXObserverAddNotification`, so no global lookup and no
/// capture is needed. The callback itself stays trivial: the element is only
/// valid for the duration of the call, so the work hops to the main queue.
private let petAXObserverCallback: AXObserverCallback = { _, element, notification, refcon in
    guard let refcon = refcon else {
        return
    }
    let controller = Unmanaged<OverlayController>
        .fromOpaque(refcon)
        .takeUnretainedValue()
    controller.petAXNotificationFired(notification as String, element: element)
}

private final class OverlayController {
    private var ringWindows: [NSWindow] = []
    private var chipWindow: NSWindow?
    private var claimChipWindow: NSWindow?
    private var claimChipView: ClaimChipView?
    private var claimChipTimer: Timer?
    /// Cadence of the live `claimChipTimer` (3.0 s with an AX observer, 0.5 s
    /// without), so a cadence change rebuilds it and a steady state does not.
    private var claimChipTimerInterval: TimeInterval = 0.0
    private var claimChipEnabled = false
    /// Sprite size and bottom padding reported by the producer; nil means the
    /// producer predates the sprite anchor and the window frame stays the box.
    private var claimChipSpriteSize: CGSize?
    private var claimChipPadBottom: CGFloat = ClaimChipSpec.defaultPadBottom
    private let claimChipGap: CGFloat = 10.0
    /// The local ask field. It takes key focus itself and never activates
    /// Hermes: the whole point of this door is that the request is typed and
    /// sent next to the pet, with the user's current app restored afterwards.
    private var summonFieldWindow: SummonFieldWindow?
    private var summonFieldView: SummonFieldView?
    private var summonFieldOpen = false
    /// The app that was frontmost when the field opened (before the companion
    /// activated itself), re-activated once the field closes.
    private var summonFieldReturnApp: NSRunningApplication?
    private let summonFieldGap: CGFloat = 8.0
    // AX notifications replace the 0.5 s reposition poll while the chip is
    // armed: the Hermes pop-out posts ~17 ms AXWindowMoved events during a
    // drag (a probe logged 86 in one drag, each with live AXPosition/AXSize).
    private var petObserver: AXObserver?
    /// CFHash of every window element the observer is attached to, so a
    /// re-attach after AXWindowCreated does not duplicate notifications.
    private var observedPetWindows = Set<Int>()
    private var observedPetPid: pid_t?
    /// The AX observer could not be created (no Hermes pid, TCC denial, OS
    /// error). The legacy 0.5 s poll then stays in charge so the chip is never
    /// left unpositioned.
    private var observerUnavailable = false
    /// Last event-driven placement, and the frame waiting behind it.
    private var repositionCoalescer: Date?
    private var pendingPetFrame: CGRect?
    private var pendingRepositionWork = false
    /// Last pet-panel frame the chip anchored to. The pet's send-message
    /// bubble is nearby, so preferring the previous origin keeps the chip from
    /// hopping between the two when both are on screen.
    private var lastPetFrame: CGRect?
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
    // Cached per-row frame step, recomputed only when the played row changes.
    private var petStepSeconds: TimeInterval = 0.0
    private var petStepRow = -1
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

    // A finished guide must not leave this process alive just because the host
    // keeps stdin open: exit shortly after the farewell once nothing is shown,
    // and keep a watchdog for any other path that ends with no UI.
    private let postFarewellExitGrace: TimeInterval = 0.75
    private let watchdogInterval: TimeInterval = 60.0
    private let watchdogIdleLimit: TimeInterval = 600.0
    private var pendingExitGeneration = 0
    /// System-wide ⌘⇧A. The desktop keybind only fires while Hermes is
    /// frontmost, so the companion owns the global one; Carbon hot keys need no
    /// extra TCC grant.
    private var summonHotKeyRef: EventHotKeyRef?
    private var summonHotKeyHandler: EventHandlerRef?
    private var watchdogTimer: Timer?
    private var lastOpUptime: TimeInterval = 0.0

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
        lastOpUptime = ProcessInfo.processInfo.systemUptime
        observeEnvironment()
        installSummonHotKey()
        startWatchdog()
        let thread = Thread { [weak self] in
            self?.readStdin()
        }
        thread.name = "butler-overlay-stdin"
        stdinThread = thread
        thread.start()

        // Claim re-assert path: a companion that was already armed (capture
        // spawn, restart, a producer that set the chip before the run loop was
        // running) must pick the observer back up and place the chip once.
        if claimChipEnabled {
            startPetObservation()
            updateClaimChipTimer()
            positionClaimChip()
        }
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
        lastOpUptime = ProcessInfo.processInfo.systemUptime
        if isShuttingDown {
            return
        }
        switch command {
        case .ring(let spec):
            cancelPendingExit()
            showRing(spec)
        case .chip(let step):
            cancelPendingExit()
            showChip(step: step)
        case .pet(let spec):
            cancelPendingExit()
            showPet(spec)
        case .claimChip(let spec):
            cancelPendingExit()
            setClaimChip(spec)
        case .launcher:
            cancelPendingExit()
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

    // MARK: Pop-out pet claim chip

    private func setClaimChip(_ spec: ClaimChipSpec) {
        guard !isShuttingDown else {
            return
        }
        claimChipEnabled = spec.enabled
        // Only a complete pair is usable: one dimension cannot centre the anchor.
        if let width = spec.spriteW, let height = spec.spriteH {
            claimChipSpriteSize = CGSize(width: width, height: height)
        } else {
            claimChipSpriteSize = nil
        }
        claimChipPadBottom = spec.padBottom
        if !spec.enabled {
            claimChipTimer?.invalidate()
            claimChipTimer = nil
            claimChipTimerInterval = 0.0
            stopPetObservation()
            hideClaimChip()
            return
        }
        // Event-driven first: the observer turns the pop-out's AX move stream
        // into chip placement, and the timer becomes a slow reconciler. If the
        // observer cannot be created the 0.5 s poll stays in charge.
        startPetObservation()
        updateClaimChipTimer()
        positionClaimChip()
    }

    // MARK: Pet AX observation

    /// Observe the Hermes app while the chip is armed.
    ///
    /// The app element carries window lifecycle notifications (a pop-out can
    /// appear after we start, so AXWindowCreated must re-attach), and every
    /// window element carries move/resize/destroyed. All of them are added
    /// with the controller as the refcon so the C callback can find us back.
    private func startPetObservation() {
        guard claimChipEnabled, !isShuttingDown, petObserver == nil else {
            return
        }
        // AXObserver sources must sit on the main run loop: AppKit windows and
        // the chip are main-thread objects. The command path is already main,
        // but never assume it.
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.startPetObservation()
            }
            return
        }
        guard let pid = hermesPid() else {
            observerUnavailable = true
            updateClaimChipTimer()
            return
        }
        var observer: AXObserver?
        let created = AXObserverCreate(pid, petAXObserverCallback, &observer)
        guard created == .success, let observer = observer else {
            // No observer: the 0.5 s poll stays in charge. Never leave the
            // chip unpositioned because AX refused a notification source.
            observerUnavailable = true
            updateClaimChipTimer()
            return
        }
        petObserver = observer
        observedPetPid = pid
        observedPetWindows.removeAll()
        observerUnavailable = false
        attachPetWindowObservers()
        CFRunLoopAddSource(CFRunLoopGetCurrent(),
                           AXObserverGetRunLoopSource(observer),
                           .commonModes)
        updateClaimChipTimer()
    }

    /// Attach per-window notifications to the app's current windows. Safe to
    /// call repeatedly: the CFHash set keeps AXWindowCreated re-attaches from
    /// doubling up on a window we already observe.
    private func attachPetWindowObservers() {
        guard let observer = petObserver, let pid = observedPetPid else {
            return
        }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        let root = AXUIElementCreateApplication(pid)
        for name in [kAXWindowCreatedNotification,
                     kAXFocusedWindowChangedNotification,
                     kAXMainWindowChangedNotification] {
            _ = AXObserverAddNotification(observer, root, name as CFString, refcon)
        }
        for window in axWindowElements(of: root) {
            let key = Int(CFHash(window))
            guard !observedPetWindows.contains(key) else {
                continue
            }
            var registered = false
            for name in [kAXWindowMovedNotification,
                         kAXWindowResizedNotification,
                         kAXUIElementDestroyedNotification] {
                let error = AXObserverAddNotification(observer,
                                                      window,
                                                      name as CFString,
                                                      refcon)
                if error == .success || error == .notificationAlreadyRegistered {
                    registered = true
                }
            }
            if registered {
                observedPetWindows.insert(key)
            }
        }
    }

    private func stopPetObservation() {
        if let observer = petObserver {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(),
                                  AXObserverGetRunLoopSource(observer),
                                  .commonModes)
        }
        petObserver = nil
        observedPetWindows.removeAll()
        observedPetPid = nil
        observerUnavailable = false
        pendingRepositionWork = false
        pendingPetFrame = nil
        repositionCoalescer = nil
    }

    /// Timer cadence: the observer makes the tick a slow reconciler (3.0 s);
    /// without it the legacy 0.5 s poll stays in charge.
    private func updateClaimChipTimer() {
        guard claimChipEnabled, !isShuttingDown else {
            return
        }
        // Observer live: reconcile slowly (3.0 s). Observer unavailable: the
        // legacy 0.5 s poll stays in charge so the chip is never unpositioned.
        let interval: TimeInterval
        if petObserver != nil && !observerUnavailable {
            interval = 3.0
        } else {
            interval = 0.5
        }
        if claimChipTimer != nil, claimChipTimerInterval == interval {
            return
        }
        claimChipTimer?.invalidate()
        claimChipTimer = nil
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            self?.positionClaimChip()
        }
        RunLoop.main.add(timer, forMode: .common)
        claimChipTimer = timer
        claimChipTimerInterval = interval
    }

    /// Entry point from the C callback. The element is only valid for the
    /// duration of the notification call, so the work is dispatched onto the
    /// main queue (never assume the observer source's thread is main).
    fileprivate func petAXNotificationFired(_ name: String, element: AXUIElement) {
        DispatchQueue.main.async { [weak self] in
            self?.processPetAXNotification(name, element: element)
        }
    }

    private func processPetAXNotification(_ name: String, element: AXUIElement) {
        guard claimChipEnabled, !isShuttingDown else {
            return
        }
        switch name {
        case kAXWindowMovedNotification, kAXWindowResizedNotification:
            // Hot path: one element, two attribute reads. The main window's
            // moves are dropped by the pet-shape filter, so dragging Hermes
            // around never moves the chip.
            guard let frame = axWindowFrame(of: element),
                  isPetShapedFrame(frame) else {
                return
            }
            lastPetFrame = frame
            scheduleEventDrivenReposition(frame)
        case kAXWindowCreatedNotification,
             kAXFocusedWindowChangedNotification,
             kAXMainWindowChangedNotification:
            // A window appeared (or focus moved to one we do not observe yet):
            // re-attach, then place the chip once from the normal poll path.
            attachPetWindowObservers()
            positionClaimChip()
        case kAXUIElementDestroyedNotification:
            // The observed element is gone; its CFHash may be reused, so start
            // the attach set over and reconcile from the normal path.
            observedPetWindows.removeAll()
            attachPetWindowObservers()
            positionClaimChip()
        default:
            break
        }
    }

    /// Throttle event-driven placements to one per 40 ms while move events
    /// stream in (a 60 Hz drag would otherwise run placement 60x/s). The
    /// newest frame wins, and the deferred block still runs after the last
    /// event so the chip lands where the pet was released.
    private func scheduleEventDrivenReposition(_ pet: CGRect) {
        pendingPetFrame = pet
        if pendingRepositionWork {
            return
        }
        pendingRepositionWork = true
        let coalesce: TimeInterval = 0.040
        let elapsed = Date().timeIntervalSince(repositionCoalescer ?? .distantPast)
        let delay = max(0.0, coalesce - elapsed)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else {
                return
            }
            self.pendingRepositionWork = false
            guard let frame = self.pendingPetFrame else {
                return
            }
            self.pendingPetFrame = nil
            guard self.claimChipEnabled, !self.isShuttingDown,
                  let primaryHeight = primaryScreenHeight() else {
                return
            }
            self.repositionCoalescer = Date()
            self.applyClaimChipFrame(petWindow: frame,
                                     primaryHeight: primaryHeight)
        }
    }

    /// Find the core Hermes pet pop-out. Identification is owner pid + shape
    /// only, never title and never window layer: the pet panel's AX title is
    /// "Hermes" (non-empty) and it is an NSPanel on CGWindow layer 3, so both
    /// an empty-title gate and a layer-0 filter miss it. AX frames and the
    /// all-layer CGWindow frames are independent sources pooled every tick;
    /// the CG source matters because AX can return nothing before its tree
    /// warms up. The main window (1280x803) is excluded by the 480 pt cap.
    private func hermesPetWindowFrame(primaryHeight: CGFloat) -> CGRect? {
        guard let pid = hermesPid() else {
            lastPetFrame = nil
            return nil
        }

        func isNearIdentical(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
            return abs(lhs.minX - rhs.minX) <= 2.0
                && abs(lhs.minY - rhs.minY) <= 2.0
                && abs(lhs.maxX - rhs.maxX) <= 2.0
                && abs(lhs.maxY - rhs.maxY) <= 2.0
        }

        // Source A: AX frames for the pet's owner pid. Titles are ignored on
        // purpose — the pop-out panel is titled "Hermes".
        var raw: [CGRect] = []
        if AXIsProcessTrusted() {
            raw.append(contentsOf: axWindowFrameList(pid: pid).map { $0.frame })
        }
        // Source B: CGWindow bounds across every layer (the panel is layer 3).
        // Pooled unconditionally: AX can return an empty tree before it warms
        // up, and the CG path does not depend on AX at all.
        raw.append(contentsOf: cgWindowFrameList(pid: pid))

        // Union both sources, collapsing frames that are identical to within
        // 2 pt on every edge (the same panel seen through AX and CGWindow).
        var merged: [CGRect] = []
        for frame in raw where !merged.contains(where: { isNearIdentical($0, frame) }) {
            merged.append(frame)
        }
        // Keep pet-shaped, on-screen candidates only. This excludes the main
        // 1280x803 window (too large) and the 248x19 strip (too short).
        let candidates = merged.filter {
            isPetShapedFrame($0) && boxIntersectsAnyScreen($0, primaryHeight: primaryHeight)
        }
        guard !candidates.isEmpty else {
            lastPetFrame = nil
            return nil
        }

        // Stick with the previous anchor when a candidate is still near it.
        // Several pet-shaped windows can sit close together (the panel and its
        // send-message bubble both pass the shape test when on screen), so
        // break ties by area: the panel is the bigger window.
        if let last = lastPetFrame {
            let anchorSlack: CGFloat = 40.0
            let nearby = candidates.filter {
                abs($0.minX - last.minX) <= anchorSlack
                    && abs($0.minY - last.minY) <= anchorSlack
            }
            if let anchored = nearby.max(by: { $0.width * $0.height < $1.width * $1.height }) {
                lastPetFrame = anchored
                return anchored
            }
        }

        // Otherwise the pet panel (240x300) is the largest pet-shaped window;
        // the bubble (84x77) loses.
        let largest = candidates.max {
            $0.width * $0.height < $1.width * $1.height
        }
        lastPetFrame = largest
        return largest
    }

    /// The sprite's box inside the pet pop-out window.
    ///
    /// pet-overlay-app.tsx lays the sprite out bottom-centred ("flex-end" plus
    /// PET_PADDING_BOTTOM), so inside a window of size W x H the sprite occupies
    /// x = minX + (W - spriteW) / 2, y = maxY - padBottom - spriteH. A producer
    /// that does not report the sprite size (older plugin, malformed fields)
    /// keeps the whole window as the anchor - today's behaviour.
    private func claimChipAnchor(window: CGRect,
                                 spriteSize: CGSize?,
                                 padBottom: CGFloat) -> CGRect {
        guard let spriteSize = spriteSize,
              spriteSize.width > 0.0, spriteSize.height > 0.0 else {
            return window
        }
        return CGRect(x: window.minX + (window.width - spriteSize.width) / 2.0,
                      y: window.maxY - padBottom - spriteSize.height,
                      width: spriteSize.width,
                      height: spriteSize.height)
    }

    private func claimChipSize() -> CGSize {
        // Icon chip: fixed size. No text measurement, so it can never widen.
        return CGSize(width: 26.0, height: 24.0)
    }

    private func claimChipFrame(nextTo pet: CGRect,
                                size: CGSize,
                                primaryHeight: CGFloat) -> CGRect? {
        guard let screen = screenContaining(pet, primaryHeight: primaryHeight) else {
            return nil
        }
        let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                 primaryHeight: primaryHeight)
        let gap = claimChipGap
        // Upper-right first, and deliberately BESIDE the head rather than above
        // it: the pet's own speech bubble ("tinkering…") renders in the strip
        // above the sprite, so a chip there would cover the pet's lines. Sitting
        // just below the sprite's top edge keeps it in the top-right quadrant
        // while leaving the bubble strip clear. The intersect guard still
        // rejects any clamped candidate that lands on the sprite.
        let candidates = [
            CGRect(x: pet.maxX + gap, y: pet.minY + gap,
                   width: size.width, height: size.height),
            CGRect(x: pet.maxX + gap, y: pet.midY - size.height / 2.0,
                   width: size.width, height: size.height),
            CGRect(x: pet.minX - gap - size.width, y: pet.minY + gap,
                   width: size.width, height: size.height),
            CGRect(x: pet.midX - size.width / 2.0, y: pet.maxY + gap,
                   width: size.width, height: size.height),
        ]
        for candidate in candidates {
            var frame = candidate
            frame.origin.x = min(max(frame.origin.x, visible.minX + 4.0),
                                 visible.maxX - frame.width - 4.0)
            frame.origin.y = min(max(frame.origin.y, visible.minY + 4.0),
                                 visible.maxY - frame.height - 4.0)
            // Separate mini window: never overlap the pet's sprite bounds.
            if !frame.intersects(pet) {
                return frame
            }
        }
        return nil
    }

    private func positionClaimChip() {
        // The slow reconciliation tick also heals the attach set: a window
        // created while the observer was not running, or an element that went
        // stale across sleep/wake, gets observed again. No-op when the
        // observer is unavailable, so the fallback poll stays pure.
        attachPetWindowObservers()
        guard claimChipEnabled, !isShuttingDown,
              let primaryHeight = primaryScreenHeight() else {
            hideClaimChip()
            return
        }
        guard let petWindow = hermesPetWindowFrame(primaryHeight: primaryHeight) else {
            // Unknown/renamed pet window: stay hidden rather than guess.
            hideClaimChip()
            return
        }
        applyClaimChipFrame(petWindow: petWindow, primaryHeight: primaryHeight)
    }

    /// Place (or move) the chip from a known pet-panel frame. `positionClaimChip`
    /// feeds it the polled anchor; the AX move/resize path feeds it the frame
    /// straight off the observed element, so a drag costs no enumeration.
    private func applyClaimChipFrame(petWindow: CGRect, primaryHeight: CGFloat) {
        guard claimChipEnabled, !isShuttingDown else {
            hideClaimChip()
            return
        }
        let size = claimChipSize()
        // Anchor to the SPRITE inside the pop-out window, not to the window:
        // the 240x300 window is mostly padding around a ~63x69 sprite, so a
        // window-frame anchor lands the chip far away from the pet.
        let pet = claimChipAnchor(window: petWindow,
                                  spriteSize: claimChipSpriteSize,
                                  padBottom: claimChipPadBottom)
        guard let frame = claimChipFrame(nextTo: pet, size: size,
                                         primaryHeight: primaryHeight) else {
            hideClaimChip()
            return
        }
        let appKitFrame = appKitRect(fromQuartz: frame, primaryHeight: primaryHeight)

        if let window = claimChipWindow, let view = claimChipView {
            if window.frame != appKitFrame {
                window.setFrame(appKitFrame, display: true)
                view.frame = NSRect(origin: .zero, size: appKitFrame.size)
                view.needsDisplay = true
            }
            return
        }

        let window = ClaimChipWindow(contentRect: appKitFrame,
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
        window.ignoresMouseEvents = false
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false

        let view = ClaimChipView(frame: NSRect(origin: .zero,
                                               size: appKitFrame.size))
        view.autoresizingMask = [.width, .height]
        view.onClick = { [weak self] in
            self?.claimChipClicked()
        }
        window.contentView = view
        claimChipWindow = window
        claimChipView = view
        window.orderFrontRegardless()
    }

    private func hideClaimChip() {
        if let window = claimChipWindow {
            window.orderOut(nil)
            window.close()
        }
        claimChipWindow = nil
        claimChipView = nil
        // The observer deliberately outlives a hidden chip: hiding happens on
        // every slow tick while the pop-out is closed or on another Space, and
        // tearing the AX observer down there would rebuild it every few
        // seconds. It is stopped only when the chip is disarmed or we shut down.
    }

    /// The chip is the local door: it opens (or closes) the in-place ask field
    /// and never raises Hermes. The request is typed and sent right here.
    private func claimChipClicked() {
        guard claimChipEnabled else {
            chipJournal("click-skipped:disabled")
            return
        }
        toggleSummonField()
    }

    // MARK: In-place ask field

    private func toggleSummonField() {
        if summonFieldOpen {
            closeSummonField(journal: "ask-cancel")
        } else {
            openSummonField()
        }
    }

    /// A 280x34 box: centred on the chip and 8 pt above it when the chip is
    /// visible, otherwise centred under the pet's sprite box. Clamped fully on
    /// screen exactly like `claimChipFrame`.
    private func summonFieldFrame(primaryHeight: CGFloat) -> CGRect? {
        let size = CGSize(width: SummonFieldView.fieldWidth,
                          height: SummonFieldView.fieldHeight)
        let anchor: CGRect
        let below: Bool
        if let chip = claimChipWindow, chip.isVisible {
            anchor = quartzRect(fromAppKit: chip.frame,
                                primaryHeight: primaryHeight)
            below = false
        } else if let petWindow = hermesPetWindowFrame(primaryHeight: primaryHeight) {
            anchor = claimChipAnchor(window: petWindow,
                                     spriteSize: claimChipSpriteSize,
                                     padBottom: claimChipPadBottom)
            below = true
        } else {
            return nil
        }
        guard let screen = screenContaining(anchor,
                                            primaryHeight: primaryHeight) else {
            return nil
        }
        let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                 primaryHeight: primaryHeight)
        let gap = summonFieldGap
        var frame = CGRect(x: anchor.midX - size.width / 2.0,
                           y: below ? anchor.maxY + gap
                                    : anchor.minY - gap - size.height,
                           width: size.width,
                           height: size.height)
        frame.origin.x = min(max(frame.origin.x, visible.minX + 4.0),
                             visible.maxX - frame.width - 4.0)
        frame.origin.y = min(max(frame.origin.y, visible.minY + 4.0),
                             visible.maxY - frame.height - 4.0)
        return frame
    }

    private func openSummonField() {
        guard !isShuttingDown, !summonFieldOpen else {
            return
        }
        guard let primaryHeight = primaryScreenHeight(),
              let frame = summonFieldFrame(primaryHeight: primaryHeight) else {
            chipJournal("ask-open-failed")
            return
        }
        let appKitFrame = appKitRect(fromQuartz: frame,
                                     primaryHeight: primaryHeight)
        // Captured BEFORE we activate: this is the app the user was working in
        // and the one that gets focus back when the field closes.
        summonFieldReturnApp = NSWorkspace.shared.frontmostApplication

        let window = SummonFieldWindow(contentRect: appKitFrame,
                                       styleMask: [.borderless],
                                       backing: .buffered,
                                       defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .statusBar
        window.collectionBehavior = [.canJoinAllSpaces,
                                     .stationary,
                                     .fullScreenAuxiliary,
                                     .ignoresCycle]
        window.ignoresMouseEvents = false
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false
        window.delegate = window
        window.onResignKey = { [weak self] in
            self?.summonFieldResignedKey()
        }

        let view = SummonFieldView(frame: NSRect(origin: .zero,
                                                 size: appKitFrame.size))
        view.autoresizingMask = [.width, .height]
        view.onSubmit = { [weak self] text in
            self?.submitSummonText(text)
        }
        view.onCancel = { [weak self] in
            self?.cancelSummonField()
        }
        window.contentView = view

        summonFieldWindow = window
        summonFieldView = view
        summonFieldOpen = true
        window.orderFrontRegardless()
        // Our own panel takes key focus. This is deliberately NOT the Hermes
        // window: the companion becomes the active app just long enough to
        // type, then the previous app is restored.
        NSApp.activate(ignoringOtherApps: true)
        window.makeKey()
        window.makeFirstResponder(view.field)
        chipJournal("ask-open")
    }

    private func closeSummonField(journal event: String?,
                                  restoreFocus: Bool = true) {
        guard summonFieldOpen || summonFieldWindow != nil else {
            return
        }
        summonFieldOpen = false
        let returnApp = summonFieldReturnApp
        summonFieldReturnApp = nil
        if let window = summonFieldWindow {
            // Detach first: orderOut/close resigns key, and the resign handler
            // must not re-enter this close.
            window.onResignKey = nil
            window.delegate = nil
            window.orderOut(nil)
            window.close()
        }
        summonFieldWindow = nil
        summonFieldView = nil
        if let event = event {
            chipJournal(event)
        }
        // Only an explicit close (submit/Esc/toggle) hands focus back. When the
        // field resigned because the user moved to another app, that app must
        // keep focus — restoring here would steal it, the very coercion this
        // door removes.
        if restoreFocus && !isShuttingDown {
            restoreFrontmostApp(returnApp)
        }
    }

    private func submitSummonText(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // Empty text is a cancel, not an empty request on the wire.
        guard !text.isEmpty else {
            closeSummonField(journal: "ask-cancel")
            return
        }
        let payload: [String: Any] = [
            "op": "summon_text",
            "text": text,
            "id": String(format: "%08x", arc4random())
        ]
        // Build the line with JSONSerialization: a quote or newline in the
        // request must never break the stdout protocol.
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: []),
              let line = String(data: data, encoding: .utf8) else {
            chipJournal("ask-submit-failed")
            closeSummonField(journal: nil)
            return
        }
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
        chipJournal("ask-submit:\(text.count)chars")
        closeSummonField(journal: nil)
    }

    private func cancelSummonField() {
        guard summonFieldOpen else {
            return
        }
        closeSummonField(journal: "ask-cancel")
    }

    private func summonFieldResignedKey() {
        guard summonFieldOpen else {
            return
        }
        // Clicking the chip moves key to its window before mouseDown runs; let
        // the chip's own toggle close the field instead of closing here and
        // immediately re-opening it.
        if let chip = claimChipWindow, NSApp.keyWindow === chip {
            return
        }
        closeSummonField(journal: "ask-cancel", restoreFocus: false)
    }

    /// Hand focus back to the app the user was in before the field opened.
    /// Never re-activates the companion itself.
    private func restoreFrontmostApp(_ app: NSRunningApplication?) {
        guard let app = app,
              !app.isTerminated,
              app.processIdentifier != ProcessInfo.processInfo.processIdentifier else {
            return
        }
        if #available(macOS 14.0, *) {
            app.activate()
        } else {
            app.activate(options: [.activateIgnoringOtherApps])
        }
    }

    // MARK: System-wide summon hot key

    /// Register ⌘⇧A with Carbon so it works from any app. The desktop keybind
    /// stays as the in-app path; when Hermes is already frontmost this handler
    /// steps aside so the two never double-open the bar.
    private func installSummonHotKey() {
        guard summonHotKeyRef == nil else {
            return
        }
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let handler: EventHandlerUPP = { _, _, userData in
            guard let userData = userData else {
                return noErr
            }
            Unmanaged<OverlayController>
                .fromOpaque(userData)
                .takeUnretainedValue()
                .summonHotKeyFired()
            return noErr
        }
        let installed = InstallEventHandler(GetApplicationEventTarget(),
                                            handler,
                                            1,
                                            &eventType,
                                            Unmanaged.passUnretained(self).toOpaque(),
                                            &summonHotKeyHandler)
        guard installed == noErr else {
            chipJournal("hotkey-handler-failed:\(installed)")
            return
        }
        let hotKeyID = EventHotKeyID(signature: OSType(0x42544C52), id: 1)  // 'BTLR'
        let status = RegisterEventHotKey(UInt32(kVK_ANSI_A),
                                         UInt32(cmdKey | shiftKey),
                                         hotKeyID,
                                         GetApplicationEventTarget(),
                                         0,
                                         &summonHotKeyRef)
        if status != noErr {
            // Another app owns ⌘⇧A: stay quiet, the in-app keybind and the chip
            // still work.
            summonHotKeyRef = nil
            chipJournal("hotkey-register-failed:\(status)")
            return
        }
        chipJournal("hotkey-installed")
    }

    private func removeSummonHotKey() {
        if let ref = summonHotKeyRef {
            UnregisterEventHotKey(ref)
        }
        summonHotKeyRef = nil
        if let handler = summonHotKeyHandler {
            RemoveEventHandler(handler)
        }
        summonHotKeyHandler = nil
    }

    private func summonHotKeyFired() {
        guard !isShuttingDown else {
            return
        }
        // A second press while the field is open closes it without emitting.
        if summonFieldOpen {
            closeSummonField(journal: "ask-cancel")
            return
        }
        if let pid = hermesPid(),
           let app = NSRunningApplication(processIdentifier: pid),
           app.isActive {
            // Hermes owns the keyboard: its own keybind opens the bar.
            return
        }
        chipJournal("hotkey-fired")
        openSummonField()
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
        petStepRow = -1
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
        scheduleExitAfterFarewell()
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

        // One full loop per state, like the desktop app: loopMs / frameCount.
        // The divisor is cached per row so a 60 Hz tick does no division.
        if petStepRow != petRow {
            petStepRow = petRow
            petStepSeconds = sheet.loopMs
                / 1000.0
                / Double(max(1, sheet.frameCount(for: petRow)))
        }
        if now - petLastFrameAdvance >= petStepSeconds {
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

    // MARK: Lifecycle watchdog

    private func startWatchdog() {
        guard watchdogTimer == nil else {
            return
        }
        let timer = Timer(timeInterval: watchdogInterval, repeats: true) { [weak self] _ in
            self?.watchdogFired()
        }
        RunLoop.main.add(timer, forMode: .common)
        watchdogTimer = timer
    }

    private func watchdogFired() {
        guard !isShuttingDown else {
            return
        }
        let idleFor = ProcessInfo.processInfo.systemUptime - lastOpUptime
        guard idleFor >= watchdogIdleLimit,
              activeRing == nil,
              petWindow == nil else {
            return
        }
        shutdown()
        NSApplication.shared.terminate(nil)
    }

    /// Invalidate any scheduled post-farewell exit. Bumping the generation is
    /// enough to make the deferred block a no-op; it also means a newer exit
    /// can be scheduled without racing the previous one.
    private func cancelPendingExit() {
        pendingExitGeneration += 1
    }

    private func scheduleExitAfterFarewell() {
        guard !isShuttingDown, activeRing == nil, chipWindow == nil else {
            return
        }
        cancelPendingExit()
        let generation = pendingExitGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + postFarewellExitGrace) { [weak self] in
            guard let self = self,
                  !self.isShuttingDown,
                  self.pendingExitGeneration == generation,
                  self.activeRing == nil,
                  self.chipWindow == nil else {
                return
            }
            self.shutdown()
            NSApplication.shared.terminate(nil)
        }
    }

    // MARK: Teardown

    /// Idempotent cleanup used by quit, EOF, and application termination.
    func shutdown() {
        guard !isShuttingDown else {
            return
        }
        isShuttingDown = true
        cancelPendingExit()
        watchdogTimer?.invalidate()
        watchdogTimer = nil
        activeRing = nil
        hideRings(close: true)
        hideChip()
        claimChipEnabled = false
        claimChipTimer?.invalidate()
        claimChipTimer = nil
        claimChipTimerInterval = 0.0
        stopPetObservation()
        removeSummonHotKey()
        closeSummonField(journal: nil, restoreFocus: false)
        hideClaimChip()
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
