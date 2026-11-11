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
//    {"op":"chip-anchor","mode":"pet"|"free"}
//      Once per hat placement change: "pet" = beside the popped-out pet panel,
//      "free" = the free-standing remembered point used when no pet panel
//      exists (e.g. the pet docked inside the Hermes window).
//    {"op":"launcher_click"}
//    {"op":"summon_text","text":"...","id":"a1b2c3d4","destination":"recent"}
//    {"op":"look_text","text":"...","id":"a1b2c3d4","destination":"recent",
//     "target":{"app":"Preview","role":"Button","label":"Export",
//               "window":"receipt.pdf","doc":"file:///...","enabled":true,
//               "box":[x,y,w,h]}}
//    {"op":"watch_ack"}   the hat menu's Acknowledge watch hit button
//
//  One global Carbon hot key: ⌘⇧A opens the plain ask field.
//
//  LOOK is a pointer gesture, not a key: press and drag the hat, release over
//  anything, and a small non-activating card appears next to the pointer with
//  what the Accessibility layer knows about that element
//  (AXUIElementCopyElementAtPosition on the system-wide element). It never
//  focuses Hermes, never takes keyboard focus and never emits a request.
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

// MARK: - LOOK hit-test (element under the cursor)

/// One element a typed LOOK request can name. Every field is optional: an
/// attribute the OS refused to read is simply absent from the wire payload
/// (and from the one-line context), never sent as a null.
private struct LookTarget {
    var app: String?
    var role: String?
    var label: String?
    var window: String?
    var doc: String?
    var enabled: Bool?
    var box: [Int]?

    /// `Button · "Export" — Preview`, with unread parts omitted.
    var contextLine: String {
        var head: [String] = []
        if let role = role, !role.isEmpty { head.append(role) }
        if let label = label, !label.isEmpty {
            head.append("\u{201C}\(label)\u{201D}")
        }
        let joined = head.joined(separator: " \u{00B7} ")
        if let app = app, !app.isEmpty {
            return joined.isEmpty ? app : joined + " \u{2014} " + app
        }
        return joined
    }

    /// The `target` object. Keys that could not be read are omitted.
    var payload: [String: Any] {
        var object: [String: Any] = [:]
        if let app = app, !app.isEmpty { object["app"] = app }
        if let role = role, !role.isEmpty { object["role"] = role }
        if let label = label, !label.isEmpty { object["label"] = label }
        if let window = window, !window.isEmpty { object["window"] = window }
        if let doc = doc, !doc.isEmpty { object["doc"] = doc }
        if let enabled = enabled { object["enabled"] = enabled }
        if let box = box, box.count == 4 { object["box"] = box }
        return object
    }
}

/// Trimmed non-empty string or nil: AX loves empty titles.
private func axNonEmpty(_ value: String?) -> String? {
    guard let value = value else { return nil }
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

/// The cursor in Quartz (top-left) space. `NSEvent.mouseLocation` is AppKit
/// bottom-left global space; the primary screen's height flips it.
private func cursorQuartzPoint() -> CGPoint? {
    guard let primaryHeight = primaryScreenHeight() else { return nil }
    let mouse = NSEvent.mouseLocation
    guard mouse.x.isFinite, mouse.y.isFinite else { return nil }
    return CGPoint(x: mouse.x, y: primaryHeight - mouse.y)
}

/// System-wide hit-test at a Quartz point. No new TCC grant: the companion's
/// existing Accessibility trust covers `AXUIElementCreateSystemWide()`.
private func axElementUnderCursor(_ point: CGPoint) -> AXUIElement? {
    let systemWide = AXUIElementCreateSystemWide()
    var element: AXUIElement?
    let error = AXUIElementCopyElementAtPosition(systemWide,
                                                 Float(point.x),
                                                 Float(point.y),
                                                 &element)
    guard error == .success else { return nil }
    return element
}

/// `AXButton` → `Button`; an exotic/unknown role is left alone.
private func axRoleName(_ raw: String?) -> String? {
    guard let raw = axNonEmpty(raw) else { return nil }
    if raw.hasPrefix("AX"), raw.count > 2 {
        return String(raw.dropFirst(2))
    }
    return raw
}

private func axEnabledAttribute(_ element: AXUIElement) -> Bool? {
    guard let raw = copyAXAttribute(element, "AXEnabled") else { return nil }
    let object = raw as AnyObject
    guard CFGetTypeID(object) == CFBooleanGetTypeID(),
          let number = object as? NSNumber else {
        return nil
    }
    return number.boolValue
}

/// Window title + document URL from up to four parent hops, because the
/// hit-tested control itself never carries either.
private func axEnclosingWindow(_ element: AXUIElement) -> (window: String?, doc: String?) {
    var windowTitle: String?
    var document: String?
    var current = element
    for _ in 0..<4 {
        guard let parent = axElement(from: copyAXAttribute(current, "AXParent")) else {
            break
        }
        current = parent
        let role = axString(current, "AXRole") ?? ""
        if role == "AXWindow" || role == "AXDocument" {
            if windowTitle == nil {
                windowTitle = axNonEmpty(axString(current, "AXTitle"))
                    ?? axNonEmpty(axString(current, "AXDescription"))
            }
            if document == nil {
                document = axNonEmpty(axString(current, "AXDocument"))
                    ?? axNonEmpty(axString(current, "AXURL"))
            }
        }
        if windowTitle != nil && document != nil {
            break
        }
    }
    return (windowTitle, document)
}

/// AXPosition/AXSize as Quartz top-left whole points, rounded like the
/// desktop's number formatter.
private func axBoxAttribute(_ element: AXUIElement) -> [Int]? {
    guard let rawPosition = copyAXAttribute(element, "AXPosition"),
          let rawSize = copyAXAttribute(element, "AXSize") else {
        return nil
    }
    let positionObject = rawPosition as AnyObject
    let sizeObject = rawSize as AnyObject
    guard CFGetTypeID(positionObject) == AXValueGetTypeID(),
          CFGetTypeID(sizeObject) == AXValueGetTypeID() else {
        return nil
    }
    let position: AXValue = unsafeBitCast(positionObject, to: AXValue.self)
    let size: AXValue = unsafeBitCast(sizeObject, to: AXValue.self)
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(position, .cgPoint, &point),
          AXValueGetValue(size, .cgSize, &dimensions),
          point.x.isFinite, point.y.isFinite,
          dimensions.width.isFinite, dimensions.height.isFinite else {
        return nil
    }
    return [Int(point.x.rounded()), Int(point.y.rounded()),
            Int(dimensions.width.rounded()), Int(dimensions.height.rounded())]
}

// MARK: - Pointer LOOK descriptor

/// What Accessibility actually knows about one point. The card and the retained
/// typed LOOK path both read this one shape, so a future caller never invents a
/// second resolution order.
private struct LookDescriptor {
    var role: String?
    var label: String?
    var app: String?
    var window: String?
    var doc: String?
    var enabled: Bool?
    var value: String?
    var box: [Int]?
    /// True when AX could not name the element under the pointer and the honest
    /// answer is the window/app it belongs to.
    var isWindowFallback = false
    /// Owning process of the resolved element. The EYE hover path uses it to
    /// reject a hit that landed in our own process (§8), which a name
    /// comparison alone can miss.
    var pid: pid_t = 0

    /// 1..5 card rows, in draw order. More than five is ellipsised by the card.
    var lines: [String] {
        var rows: [String] = []
        if isWindowFallback {
            rows.append("Window \u{00B7} " + (axNonEmpty(app) ?? "frontmost app"))
            if let window = axNonEmpty(window), window != axNonEmpty(app) {
                rows.append(window)
            }
        } else {
            if let role = axNonEmpty(role) {
                if let label = axNonEmpty(label) {
                    rows.append("\(role) \u{00B7} \u{201C}\(label)\u{201D}")
                } else {
                    rows.append(role)
                }
            } else if let label = axNonEmpty(label) {
                rows.append("\u{201C}\(label)\u{201D}")
            }
            if let app = axNonEmpty(app) {
                var row = app
                if let window = axNonEmpty(window) {
                    row += " \u{00B7} " + window
                }
                if let doc = axNonEmpty(doc) {
                    row += " \u{2014} " + doc
                }
                rows.append(row)
            } else if let window = axNonEmpty(window) {
                rows.append(window)
            }
            if enabled == false {
                rows.append("disabled")
            }
            // Static text carries its words in AXValue, so the label row can
            // already be that same string: do not spend a second line on it.
            if let value = axNonEmpty(value),
               value != axNonEmpty(label) {
                rows.append("Value: \u{201C}\(value)\u{201D}")
            }
        }
        return Array(rows.prefix(5))
    }

    /// `Button · "Export" — Preview`, used by the retained one-line typed path.
    var contextLine: String {
        let rows = lines
        guard let first = rows.first else { return "" }
        if !isWindowFallback, rows.count > 1, let app = axNonEmpty(app) {
            return first + " \u{2014} " + app
        }
        return first
    }
}

/// Container roles describe *where* a control lives, not the control itself.
/// A Chromium/Electron button hit-test comes back as a bare `AXGroup` with no
/// label (measured on the Hermes window at (98,46)), so an unlabelled container
/// must never end the search.
private let axContainerRoles: Set<String> = [
    "AXGroup", "AXPane", "AXScrollArea", "AXSplitGroup", "AXLayoutArea",
    "AXLayoutItem", "AXUnknown", "AXGenericElement", "AXList", "AXTable",
    "AXRow", "AXColumn", "AXOutline", "AXCanvas", "AXWebArea", "AXImage",
    "AXStaticText", "AXHeading",
]

/// Text in AXValue is only a read-out line for string values; numbers and
/// ranges are not worth showing on a glance card.
private func axValueString(_ element: AXUIElement) -> String? {
    guard let raw = copyAXAttribute(element, "AXValue"),
          let text = raw as? String,
          let trimmed = axNonEmpty(text) else {
        return nil
    }
    return String(trimmed.prefix(120))
}

/// The label a human would read: title, then description, then (for static
/// text, which carries its words in AXValue) the value itself.
private func axLookLabel(_ element: AXUIElement) -> String? {
    if let title = axNonEmpty(axString(element, "AXTitle")) {
        return title
    }
    if let description = axNonEmpty(axString(element, "AXDescription")) {
        return description
    }
    let role = axString(element, "AXRole") ?? ""
    if role == "AXStaticText" || role == "AXHeading" {
        return axValueString(element)
    }
    return nil
}

private func axElementList(_ element: AXUIElement, attribute: String) -> [AXUIElement] {
    guard let raw = copyAXAttribute(element, attribute) as? [Any] else {
        return []
    }
    return raw.compactMap { axElement(from: $0) }
}

/// Resolve the Accessibility element at a Quartz point into the card read-out.
///
/// The order fixes the measured Chromium/Electron defect:
///   1. the hit element when it has a label or a meaningful (non-container) role;
///   2. down up to 2 `AXChildren` levels for a labelled child whose box
///      contains the point;
///   3. up to 4 `AXParent` levels for the first labelled ancestor;
///   4. `Window · <frontmost app>`, never a bare `Group`.
private func lookElementDescriptor(at point: CGPoint) -> LookDescriptor {
    return lookDescriptor(for: axElementUnderCursor(point), at: point)
}

/// Compose the read-out from an already hit-tested element.
///
/// Two doors share this body: the system-wide hit-test (after our own windows
/// are parked) and the app-scoped hover preview of INSPECT MODE, whose overlay
/// is topmost everywhere and would otherwise answer every point.
private func lookDescriptor(for hit: AXUIElement?, at point: CGPoint) -> LookDescriptor {
    let frontmost = axNonEmpty(NSWorkspace.shared.frontmostApplication?.localizedName)
    guard let hit = hit else {
        return LookDescriptor(role: nil, label: nil, app: frontmost,
                              window: nil, doc: nil, enabled: nil, value: nil,
                              box: nil, isWindowFallback: true)
    }

    func boxContains(_ element: AXUIElement) -> Bool {
        guard let box = axBoxAttribute(element), box.count == 4 else {
            return false
        }
        let rect = CGRect(x: CGFloat(box[0]), y: CGFloat(box[1]),
                          width: CGFloat(box[2]), height: CGFloat(box[3]))
        return rect.contains(point)
    }

    func labelledDescendant(_ element: AXUIElement, levels: Int) -> AXUIElement? {
        guard levels > 0 else { return nil }
        let children = axElementList(element, attribute: "AXChildren")
        for child in children
        where axNonEmpty(axLookLabel(child)) != nil && boxContains(child) {
            return child
        }
        for child in children {
            if let found = labelledDescendant(child, levels: levels - 1) {
                return found
            }
        }
        return nil
    }

    func labelledAncestor(_ element: AXUIElement, levels: Int) -> AXUIElement? {
        var current = element
        for _ in 0..<levels {
            guard let parent = axElement(from: copyAXAttribute(current, "AXParent")) else {
                return nil
            }
            if axNonEmpty(axLookLabel(parent)) != nil {
                return parent
            }
            current = parent
        }
        return nil
    }

    let hitRoleRaw = axString(hit, "AXRole")
    let hitLabel = axNonEmpty(axLookLabel(hit))
    let hitIsContainer = hitRoleRaw == nil || axContainerRoles.contains(hitRoleRaw ?? "")
    var chosen = hit
    if hitLabel == nil && hitIsContainer {
        if let child = labelledDescendant(hit, levels: 2) {
            chosen = child
        } else if let parent = labelledAncestor(hit, levels: 4) {
            chosen = parent
        } else {
            // Nothing named under the pointer: say which window this is instead
            // of leaking "Group" as if it were the answer.
            var pid: pid_t = 0
            var app = frontmost
            if AXUIElementGetPid(hit, &pid) == .success, pid > 0 {
                app = axNonEmpty(NSRunningApplication(processIdentifier: pid)?.localizedName)
                    ?? app
            }
            let enclosing = axEnclosingWindow(hit)
            return LookDescriptor(role: "Window", label: nil, app: app,
                                  window: enclosing.window, doc: enclosing.doc,
                                  enabled: nil, value: nil,
                                  box: axBoxAttribute(hit),
                                  isWindowFallback: true,
                                  pid: pid)
        }
    }

    var pid: pid_t = 0
    var app = frontmost
    if AXUIElementGetPid(chosen, &pid) == .success, pid > 0 {
        app = axNonEmpty(NSRunningApplication(processIdentifier: pid)?.localizedName) ?? app
    }
    let enclosing = axEnclosingWindow(chosen)
    return LookDescriptor(role: axRoleName(axString(chosen, "AXRole")),
                          label: axNonEmpty(axLookLabel(chosen)),
                          app: app,
                          window: enclosing.window,
                          doc: enclosing.doc,
                          enabled: axEnabledAttribute(chosen),
                          value: axValueString(chosen),
                          box: axBoxAttribute(chosen),
                          pid: pid)
}

/// True when the hit element belongs to THIS process.
///
/// The hat is a status-level window and the INSPECT overlay is a screenSaver-
/// level one, so wherever they happen to be, AX hit testing finds them before
/// anything underneath. Reporting "Window · ButlerOverlay" is never a useful
/// answer, so the caller retries with our windows parked and finally falls back
/// to the active app's window. The pid is the exact test (a descriptor whose AX
/// pid read failed falls through to the app-name comparison).
private func lookDescriptorIsSelf(_ descriptor: LookDescriptor) -> Bool {
    if descriptor.pid != 0,
       descriptor.pid == ProcessInfo.processInfo.processIdentifier {
        return true
    }
    guard let app = descriptor.app else { return false }
    let me = ProcessInfo.processInfo.processName
    let mine = NSRunningApplication.current.localizedName ?? me
    return app == mine || app == me
}

/// The app the user is actually working in: the menu-bar owner, which accessory
/// helpers like this one never are.
private func activeWorkAppName() -> String? {
    if let app = NSWorkspace.shared.menuBarOwningApplication,
       app.processIdentifier != ProcessInfo.processInfo.processIdentifier {
        return axNonEmpty(app.localizedName)
    }
    return axNonEmpty(NSWorkspace.shared.frontmostApplication?.localizedName)
}

/// A descriptor for the active (non-self) app's focused window. Last resort when
/// the hit-test can only see our own window.
private func lookDescriptorForActiveApp() -> LookDescriptor {
    let app = activeWorkAppName()
    var window: String?
    var doc: String?
    if let pid = NSWorkspace.shared.menuBarOwningApplication?.processIdentifier,
       pid != ProcessInfo.processInfo.processIdentifier {
        let root = AXUIElementCreateApplication(pid)
        if let focused = axElement(from: copyAXAttribute(root, "AXFocusedWindow")) {
            let enclosing = axEnclosingWindow(focused)
            window = enclosing.window
            doc = enclosing.doc
        }
    }
    return LookDescriptor(role: "Window", label: nil, app: app, window: window,
                          doc: doc, enabled: nil, value: nil, box: nil,
                          isWindowFallback: true)
}

/// Hit-test the cursor and compose the LOOK context + target for the retained
/// typed path. Resolution lives in `lookElementDescriptor` so both doors share
/// one path.
private func axLookTargetUnderCursor() -> (context: String, target: LookTarget) {
    let frontmost = axNonEmpty(NSWorkspace.shared.frontmostApplication?.localizedName)
    guard let point = cursorQuartzPoint() else {
        return ("Window: " + (frontmost ?? "unknown"),
                LookTarget(app: frontmost))
    }
    let descriptor = lookElementDescriptor(at: point)
    let target = LookTarget(app: descriptor.app,
                            role: descriptor.role,
                            label: descriptor.label,
                            window: descriptor.window,
                            doc: descriptor.doc,
                            enabled: descriptor.enabled,
                            box: descriptor.box)
    let context = target.contextLine
    if !context.isEmpty {
        return (context, target)
    }
    return ("Window: " + (descriptor.window ?? descriptor.app ?? frontmost ?? "unknown"),
            target)
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

// MARK: - Ambient WATCH transport

/// One JSON object per line on stdout. Every event line the Python side reads
/// (watch events, `chip-anchor`, …) goes through here, so a bad payload costs
/// one dropped line instead of the protocol.
private func writeEventLine(_ payload: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
        logStderr("butler-overlay: stdout line encode failed")
        return
    }
    // One write per line: two writes could interleave with the other writer on
    // the same pipe and split a JSON line in half.
    FileHandle.standardOutput.write(data + Data([0x0a]))
}

/// One JSON object per line for the watch protocol: `watch-event`, `watch-hit`,
/// `watch-stopped`, and the synchronous `watch_start` reply.
private func writeWatchLine(_ payload: [String: Any]) {
    writeEventLine(payload)
}

/// A start rejection, carrying the producer's token when it sent one so a
/// timed-out start's late reply can never answer a later watch_start.
private func watchFailure(_ code: String, token: String?) -> [String: Any] {
    var payload: [String: Any] = ["watch_error": code]
    if let token = token {
        payload["token"] = token
    }
    return payload
}

/// Coalescing contract: at most 4 watch-event lines per second, and an
/// identical event (same kind/role/label/window/value) inside 150 ms is a
/// duplicate. Both gates sit AFTER the condition check, so a real
/// "Export complete" can never be rate-limited away.
private let watchMaxEventsPerSecond = 4
private let watchDuplicateWindow: TimeInterval = 0.15
private let watchRateWindow: TimeInterval = 1.0
private let watchLabelLimit = 120
private let watchLivenessInterval: TimeInterval = 2.0

/// C trampoline for the watched app, mirroring `petAXObserverCallback`: the
/// controller is the refcon on every `AXObserverAddNotification`. The element
/// is only valid during the call, so the work hops to the main queue.
private let watchAXObserverCallback: AXObserverCallback = { _, element, notification, refcon in
    guard let refcon = refcon else {
        return
    }
    let controller = Unmanaged<OverlayController>
        .fromOpaque(refcon)
        .takeUnretainedValue()
    controller.watchAXNotificationFired(notification as String, element: element)
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

/// One ambient watch request: an app to observe and an optional condition.
private struct WatchStartSpec {
    let app: String
    let until: String?
    /// Producer-side start token, echoed on every line this watch emits so a
    /// late line from the REPLACED watch can never be mistaken for the new one.
    let token: String?
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

/// Which anchor the hat is on: beside the popped-out pet panel, or the
/// free-standing remembered screen point used when no pet panel exists (the
/// default fresh-install state, where the pet is docked inside the Hermes
/// window). Reported on stdout as `chip-anchor` so the Python side can surface
/// which state the companion is in.
private enum ClaimChipAnchorMode: String {
    case pet
    case free
}

private enum OverlayCommand {
    case ring(RingSpec)
    case chip(Int)
    case pet(PetSpec)
    case claimChip(ClaimChipSpec)
    case watchStart(WatchStartSpec)
    case watchStop
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

    case "watch_start":
        guard let rawApp = stringValue(object["app"]) else {
            logMalformedOnce(line: line, reason: "watch_start has an invalid app")
            return nil
        }
        let app = rawApp.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !app.isEmpty else {
            logMalformedOnce(line: line, reason: "watch_start has a blank app")
            return nil
        }
        var until: String?
        if let rawUntil = object["until"], !(rawUntil is NSNull) {
            guard let text = stringValue(rawUntil) else {
                logMalformedOnce(line: line, reason: "watch_start has an invalid until")
                return nil
            }
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            until = trimmed.isEmpty ? nil : trimmed
        }
        var token: String?
        if let rawToken = object["token"], !(rawToken is NSNull) {
            guard let text = stringValue(rawToken) else {
                logMalformedOnce(line: line, reason: "watch_start has an invalid token")
                return nil
            }
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            token = trimmed.isEmpty ? nil : trimmed
        }
        return .watchStart(WatchStartSpec(app: app, until: until, token: token))

    case "watch_stop":
        return .watchStop

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

/// The free-standing hat's anchor: a remembered point on screen, used whenever
/// no popped-out pet panel resolves. It is what keeps the hat — LOOK, the ask
/// field and the watch menu — reachable in the fresh-install state where the
/// pet is docked inside the Hermes window.
///
/// The point is remembered in the companion's OWN preferences domain, so a
/// user's placement survives restarts without the Python side knowing the
/// geometry.
private enum ClaimChipFreeAnchor {
    /// The defaults key: "x,y" in AppKit global coordinates (bottom-left
    /// origin of the primary display, y upward).
    static let defaultsKey = "butlerHatFreeAnchor"
    /// Where the default point sits, in points in from the screen's right and
    /// bottom edges. 40 pt clears the Dock on a default layout.
    static let inset: CGFloat = 40.0
    /// How long a PET-ANCHORED hat waits before it goes free: a pop-out being
    /// dragged, resized or created must not flicker the hat to the corner.
    /// A hat that was never pet-anchored (fresh install, docked pet) skips the
    /// grace and appears at the free point at once — waiting here would leave
    /// the hat invisible, which is the defect this anchor exists to fix.
    static let petGrace: TimeInterval = 3.0
    /// A remembered point is clamped this far inside the screen's visible
    /// frame, the same margin the pet-relative placement uses.
    static let edgeMargin: CGFloat = 4.0

    /// `butlerHatFreeAnchor` parsed back into a point; a malformed or absent
    /// value simply means "never placed", never a crash or a lost hat.
    static func remembered(from defaults: UserDefaults) -> NSPoint? {
        guard let raw = defaults.string(forKey: defaultsKey) else {
            return nil
        }
        let parts = raw.split(separator: ",")
        guard parts.count == 2,
              let x = Double(parts[0].trimmingCharacters(in: .whitespaces)),
              let y = Double(parts[1].trimmingCharacters(in: .whitespaces)),
              x.isFinite, y.isFinite else {
            return nil
        }
        return NSPoint(x: x, y: y)
    }

    /// Persisted on release, never mid-drag: a drag that ends in a cancel must
    /// not rewrite the remembered point.
    static func remember(_ point: NSPoint, in defaults: UserDefaults) {
        guard point.x.isFinite, point.y.isFinite else {
            return
        }
        defaults.set("\(point.x),\(point.y)", forKey: defaultsKey)
    }

    /// The main screen's bottom-right corner, inset by `inset` from the right
    /// edge and from the bottom edge. `visibleFrame` already excludes the menu
    /// bar and the Dock.
    static func defaultOrigin(size: CGSize) -> NSPoint? {
        guard let screen = primaryScreen() else {
            return nil
        }
        let visible = screen.visibleFrame
        return NSPoint(x: visible.maxX - inset - size.width,
                       y: visible.minY + inset)
    }
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
    /// While dragging, the hat becomes eyes so "I am reading the pointer" is
    /// visible without a tooltip or a separate mode indicator.
    private static let lookGlyph = "👀"
    private static let glyphFont = NSFont.systemFont(ofSize: 13.0)
    private static let lookGlyphFont = NSFont.systemFont(ofSize: 14.0)
    /// A press shorter than this (in points) is a click; more is a LOOK drag.
    static let dragThreshold: CGFloat = 4.0
    /// Hover state drives both the pointing-hand cursor and a brighter chip, so
    /// "this is clickable" survives even when AppKit will not swap the cursor
    /// for a background (inactive) app's window.
    private var hovering = false
    private var hoverTracking: NSTrackingArea?
    /// True from the first moved point until mouseUp; the controller then drops
    /// the read-out card at the release point.
    private(set) var lookMode = false
    /// Set while the hat is free-standing, where the same press MOVES it. A
    /// drag then keeps the hat glyph, because claiming to read the pointer
    /// while the badge is simply being placed would be a lie.
    var dragMovesTheHat = false
    private var pressStart: NSPoint?
    private var dragging = false
    var onClick: (() -> Void)?
    /// The hat's second door: a right-click arms INSPECT MODE or, with Option,
    /// opens the LOOK/watch menu. The point is in this view's coordinates.
    var onRightClick: ((NSPoint) -> Void)?
    /// Fires on mouseDown, before a drag exists, so the placement timer stops
    /// the moment the user grabs the hat.
    var onPressBegan: ((NSPoint) -> Void)?
    var onDragBegan: ((NSPoint) -> Void)?
    var onDragMoved: ((NSPoint) -> Void)?
    var onDragEnded: ((NSPoint) -> Void)?

    /// Global AppKit space (bottom-left origin), independent of the hat window
    /// moving under the cursor during the drag.
    private func screenPoint(for event: NSEvent) -> NSPoint {
        guard let window = window else { return NSEvent.mouseLocation }
        return window.convertPoint(toScreen: event.locationInWindow)
    }

    /// The controller calls this when a press lost its mouseUp (window hidden
    /// or reordered mid-gesture) so a later hover does not carry stale state.
    func cancelPendingPress() {
        pressStart = nil
        dragging = false
        lookMode = false
        needsDisplay = true
    }

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
        // Decide press-vs-drag on release: a click still opens the ask field,
        // movement past `dragThreshold` turns the same press into LOOK.
        let point = screenPoint(for: event)
        pressStart = point
        dragging = false
        onPressBegan?(point)
        chipJournal("press")
    }

    override func mouseDragged(with event: NSEvent) {
        guard let start = pressStart else { return }
        let point = screenPoint(for: event)
        let dx = point.x - start.x
        let dy = point.y - start.y
        let movement = (dx * dx + dy * dy).squareRoot()
        if !dragging && movement > ClaimChipView.dragThreshold {
            dragging = true
            // The eye glyph means "I am reading the pointer"; a free hat being
            // placed is not reading anything, so it stays a hat.
            lookMode = !dragMovesTheHat
            needsDisplay = true
            onDragBegan?(start)
            chipJournal("drag-begin")
        }
        if dragging {
            onDragMoved?(point)
        }
    }

    /// Right-click is the menu door. Acted on mouseDown so the menu tracks
    /// immediately.
    override func rightMouseDown(with event: NSEvent) {
        pressStart = nil
        dragging = false
        let point = convert(event.locationInWindow, from: nil)
        onRightClick?(point)
    }

    override func mouseUp(with event: NSEvent) {
        // A mouseUp without our press (window hid/reordered mid-gesture) must
        // not toggle the ask field from nowhere.
        guard pressStart != nil else { return }
        let point = screenPoint(for: event)
        let wasDragging = dragging
        dragging = false
        pressStart = nil
        if wasDragging {
            lookMode = false
            needsDisplay = true
            onDragEnded?(point)
            chipJournal("drag-end")
        } else {
            chipJournal("click")
            onClick?()
        }
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

        let glyph = lookMode ? ClaimChipView.lookGlyph : ClaimChipView.glyph
        let attributes: [NSAttributedString.Key: Any] = [
            .font: lookMode ? ClaimChipView.lookGlyphFont : ClaimChipView.glyphFont,
            .foregroundColor: NSColor.white
        ]
        let glyphSize = (glyph as NSString).size(withAttributes: attributes)
        let glyphRect = NSRect(x: (bounds.width - glyphSize.width) / 2.0,
                               y: (bounds.height - glyphSize.height) / 2.0,
                               width: glyphSize.width,
                               height: glyphSize.height)
        (glyph as NSString).draw(in: glyphRect, withAttributes: attributes)
    }
}

// MARK: - Pointer LOOK card (DEAD since the drag parity)

/// DEAD SINCE THE DRAG PARITY: no caller. A hat drag arms INSPECT MODE and
/// confirms on release through `resolveInspectClick`, so the follower/card pair
/// below is never raised. Kept (with `resolveLookDrop`, `lookCardFrame`,
/// `presentLookCard`) instead of deleting a large Swift block hours before a
/// submission. Delete the whole block once the demo is over.
///
/// The read-out panel. It must NEVER become key: the user's app keeps typing
/// focus while the card is on screen, and a click anywhere simply dismisses it.
private final class LookCardWindow: NSPanel {
    /// The card is a glance, not a document: it goes away on its own.
    static let autoDismissInterval: TimeInterval = 6.0

    override var canBecomeKey: Bool {
        return false
    }

    override var canBecomeMain: Bool {
        return false
    }
}

/// A small fixed-width card of up to five lines. No buttons, no footer, no chat
/// link: it only reports what Accessibility returned for the drop point.
private final class LookCardView: NSView {
    static let cardWidth: CGFloat = 320.0
    static let maxLines = 5
    private static let horizontalInset: CGFloat = 14.0
    private static let verticalInset: CGFloat = 9.0
    private static let lineHeight: CGFloat = 16.0
    private static let lineGap: CGFloat = 2.0
    private static let firstLineFont = NSFont.systemFont(ofSize: 12.5,
                                                        weight: .semibold)
    private static let bodyFont = NSFont.systemFont(ofSize: 11.5)

    static func height(for lines: [String]) -> CGFloat {
        let count = max(1, min(lines.count, maxLines))
        return verticalInset * 2.0 + CGFloat(count) * lineHeight
            + CGFloat(max(0, count - 1)) * lineGap
    }

    init(frame: NSRect, lines: [String]) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        for (index, line) in lines.prefix(LookCardView.maxLines).enumerated() {
            let label = NSTextField(labelWithString: line)
            label.font = index == 0 ? LookCardView.firstLineFont
                                    : LookCardView.bodyFont
            label.textColor = index == 0
                ? NSColor.white
                : NSColor(white: 0.78, alpha: 1.0)
            // The hard cap is enforced here: one visual line per row, tail
            // ellipsis, never a wrapped second line that grows the card.
            label.lineBreakMode = .byTruncatingTail
            label.maximumNumberOfLines = 1
            label.isSelectable = false
            label.refusesFirstResponder = true
            label.frame = NSRect(x: LookCardView.horizontalInset,
                                 y: LookCardView.verticalInset
                                    + CGFloat(index) * (LookCardView.lineHeight
                                                        + LookCardView.lineGap),
                                 width: max(0.0, frame.width
                                            - LookCardView.horizontalInset * 2.0),
                                 height: LookCardView.lineHeight)
            addSubview(label)
        }
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
                                xRadius: 10.0,
                                yRadius: 10.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 30.0 / 255.0,
                alpha: 0.96).setFill()
        path.fill()
        NSColor(red: 122.0 / 255.0,
                green: 134.0 / 255.0,
                blue: 255.0 / 255.0,
                alpha: 0.55).setStroke()
        path.lineWidth = 1.0
        path.stroke()
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
    /// ⌘Return can arrive as a key equivalent before the field editor ever
    /// turns it into `insertNewline:`. This is the second delivery path; the
    /// view's own guard makes the two paths emit exactly one request.
    var onCommandReturn: (() -> Void)?

    override var canBecomeKey: Bool {
        return true
    }

    override var canBecomeMain: Bool {
        return false
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command),
           (event.keyCode == 36 || event.keyCode == 76) {
            onCommandReturn?()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    func windowDidResignKey(_ notification: Notification) {
        onResignKey?()
    }
}

/// Which Butler chat a typed request goes to. The modifier held at Return time
/// is the whole choice: per-send, never persisted.
private enum SummonDestination: String {
    case recent
    case new
}

/// The typing box that appears beside the pet. Same dark fill and indigo border
/// as the claim chip, one single-line `NSTextField`, Enter/Return to send and
/// Escape to cancel. The destination is chosen inside the field: a muted
/// `⌘↵ new` hint sits at its right edge, so ↵ continues the most recent Butler
/// chat and ⌘↵ starts a fresh one without any second UI.
private final class SummonFieldView: NSView, NSTextFieldDelegate {
    static let fieldWidth: CGFloat = 280.0
    static let fieldHeight: CGFloat = 34.0
    /// LOOK mode adds a muted target line above the input, so the same pill
    /// gets one extra 18 pt row.
    static let lookFieldHeight: CGFloat = 52.0
    private static let inset: CGFloat = 12.0
    private static let textHeight: CGFloat = 22.0
    private static let contextHeight: CGFloat = 14.0
    /// 4 (top) + 14 (line) = the 18 pt row LOOK adds above the 34 pt input
    /// band, so the context never dips into the input row.
    private static let contextTopInset: CGFloat = 4.0
    /// Room reserved at the right of the pill for the inline destination hint.
    private static let hintWidth: CGFloat = 58.0
    private static let hintGap: CGFloat = 6.0
    private static let hintHeight: CGFloat = 14.0

    let field = NSTextField(frame: .zero)
    /// The discoverable half of the ⌘↵ choice: a muted, right-aligned label.
    /// A label is never editable or focusable, so it cannot steal the caret.
    let hint = NSTextField(labelWithString: "⌘↵ new")
    /// The muted one-line "what is under the cursor" above the input. Hidden
    /// for the plain ask door so the box keeps its exact 34 pt shape.
    private let contextLabel = NSTextField(labelWithString: "")
    private(set) var lookContext: String?
    var onSubmit: ((String, SummonDestination) -> Void)?
    var onCancel: (() -> Void)?

    /// Window height for a door: LOOK needs the extra context row.
    static func height(forLook look: Bool) -> CGFloat {
        return look ? lookFieldHeight : fieldHeight
    }
    /// One Return press can arrive twice (key equivalent, then command
    /// selector). The first delivery wins; a double send is a bug.
    private var submitInFlight = false

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

        // Muted ~55% white at 10.5 pt: visible when looked for, never competing
        // with the placeholder.
        hint.font = NSFont.systemFont(ofSize: 10.5)
        hint.textColor = NSColor.white.withAlphaComponent(0.55)
        hint.alignment = .right
        hint.isEditable = false
        hint.isSelectable = false
        hint.isBezeled = false
        hint.isBordered = false
        hint.drawsBackground = false
        hint.focusRingType = .none
        hint.refusesFirstResponder = true
        hint.setAccessibilityLabel("Command-Return starts a new Butler chat")
        addSubview(hint)

        // Same muted 10.5 pt treatment as the destination hint; a label can
        // never take the caret away from the input.
        contextLabel.font = NSFont.systemFont(ofSize: 10.5)
        contextLabel.textColor = NSColor.white.withAlphaComponent(0.55)
        contextLabel.alignment = .left
        contextLabel.lineBreakMode = .byTruncatingTail
        contextLabel.usesSingleLineMode = true
        contextLabel.cell?.lineBreakMode = .byTruncatingTail
        contextLabel.cell?.usesSingleLineMode = true
        contextLabel.isEditable = false
        contextLabel.isSelectable = false
        contextLabel.isBezeled = false
        contextLabel.isBordered = false
        contextLabel.drawsBackground = false
        contextLabel.focusRingType = .none
        contextLabel.refusesFirstResponder = true
        contextLabel.isHidden = true
        contextLabel.setAccessibilityLabel("Look context")
        addSubview(contextLabel)
    }

    /// Switch the box into LOOK mode (a non-nil context) or back to the plain
    /// ask door. The placeholder is the contract the user sees.
    func configureForLook(context: String?) {
        lookContext = context
        if let context = context {
            contextLabel.stringValue = context
            contextLabel.isHidden = false
            field.placeholderString = "Ask about this…"
            field.setAccessibilityLabel("Ask about this control")
        } else {
            contextLabel.stringValue = ""
            contextLabel.isHidden = true
            field.placeholderString = "Ask the butler…"
            field.setAccessibilityLabel("Ask the butler")
        }
        needsLayout = true
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
        let hintX = bounds.width - SummonFieldView.inset - SummonFieldView.hintWidth
        hint.frame = NSRect(x: hintX,
                            y: (bounds.height - SummonFieldView.hintHeight) / 2.0,
                            width: SummonFieldView.hintWidth,
                            height: SummonFieldView.hintHeight)
        // The input keeps its placeholder but yields room to the hint.
        let inputWidth = hintX - SummonFieldView.hintGap - SummonFieldView.inset
        // LOOK keeps the input on the 34 pt bottom row and paints the context
        // line above it; the plain door stays vertically centred.
        let inputY = lookContext == nil
            ? (bounds.height - SummonFieldView.textHeight) / 2.0
            : (SummonFieldView.fieldHeight - SummonFieldView.textHeight) / 2.0
        if lookContext != nil {
            contextLabel.frame = NSRect(
                x: SummonFieldView.inset,
                y: bounds.height - SummonFieldView.contextTopInset
                    - SummonFieldView.contextHeight,
                width: max(0.0, bounds.width - SummonFieldView.inset * 2.0),
                height: SummonFieldView.contextHeight)
        } else {
            contextLabel.frame = .zero
        }
        field.frame = NSRect(x: SummonFieldView.inset,
                             y: inputY,
                             width: max(0.0, inputWidth),
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

    /// Emit at most one request per Return press. The first delivery wins, so a
    /// ⌘Return that reached us both as a key equivalent and as a command
    /// selector still sends exactly once.
    @discardableResult
    func emitSubmit(destination: SummonDestination) -> Bool {
        guard !submitInFlight else {
            return true
        }
        submitInFlight = true
        onSubmit?(field.stringValue, destination)
        return true
    }

    /// A fresh field (or new text) means the next Return is a new press.
    private func resetSubmitGuard() {
        submitInFlight = false
    }

    func controlTextDidChange(_ notification: Notification) {
        resetSubmitGuard()
    }

    /// Return/Enter sends, Escape cancels. `doCommandBy` is the only place a
    /// single-line field reports those keys before they beep or end editing.
    func control(_ control: NSControl,
                 textView: NSTextView,
                 doCommandBy commandSelector: Selector) -> Bool {
        if commandSelector == #selector(NSResponder.insertNewline(_:))
            || commandSelector == #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:)) {
            // The selector does not carry the modifier; the event that produced
            // it does. ↵ continues the newest chat, ⌘↵ starts a fresh one.
            let commandHeld = NSApp.currentEvent?.modifierFlags.contains(.command) ?? false
            emitSubmit(destination: commandHeld ? .new : .recent)
            return true
        }
        if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
            onCancel?()
            return true
        }
        return false
    }
}

// MARK: - INSPECT MODE (right-click the hat → a click-catching overlay)

/// Geometry and cadence for INSPECT MODE, fixed on purpose.
///
/// The badge is centred +18/+26 from the pointer: far enough away that the pill
/// (and the window it lives in) is never the element under test, and the
/// placement flips it around the cursor at a screen edge so a clamp can never
/// leave it on the point (§8 self-hit class of defect).
private enum InspectModeSpec {
    static let badgeOffsetX: CGFloat = 18.0
    static let badgeOffsetY: CGFloat = 26.0
    static let badgeFontSize: CGFloat = 11.0
    /// 15 pt per row for an 11 pt line: the 👀 glyph is taller than the Latin
    /// text and must not clip inside the pill.
    static let badgeLineHeight: CGFloat = 15.0
    static let badgeLineGap: CGFloat = 2.0
    static let badgeInsetX: CGFloat = 9.0
    static let badgeInsetY: CGFloat = 6.0
    /// Widen to the longest line, never below 180 nor above 320, so the pill
    /// stays a glance and the second line never wraps.
    static let badgeMinWidth: CGFloat = 180.0
    static let badgeMaxWidth: CGFloat = 320.0
    /// "Cap the text at 30 characters, ellipsise."
    static let badgeMaxCharacters = 30
    /// 10 Hz pointer follow: the badge rides the cursor, the AX read is
    /// movement-gated so this never becomes 10 reads a second.
    static let tickInterval: TimeInterval = 1.0 / 10.0
    /// A pointer that moved less than this keeps the last tested point: menus,
    /// tooltips and the badge itself jitter by a point or two.
    static let moveThreshold: CGFloat = 6.0
    /// No click for a minute cancels the mode on its own.
    static let timeout: TimeInterval = 60.0
    /// The badge's fixed first line. The two arm modes confirm differently
    /// (a click vs the release that ends a drag), so the offer is spelled out
    /// per mode instead of pretending both are clicks.
    static let clickTitle = "👀 click to ask · esc / right-click to cancel"
    static let releaseTitle = "👀 release to ask · esc to cancel"
}

/// Hard cap + tail ellipsis for the live target line.
private func inspectTargetText(_ text: String,
                               limit: Int = InspectModeSpec.badgeMaxCharacters) -> String {
    guard limit > 1, text.count > limit else { return text }
    return String(text.prefix(limit - 1)) + "\u{2026}"
}

/// The descriptor as one line: `Button · "Merge pull request"`.
///
/// An unnamed control still says WHERE it is (window, then app) instead of
/// printing a bare "Window" — the §8 "Group alone is never an answer" rule.
private func inspectTargetLine(_ descriptor: LookDescriptor) -> String {
    var text: String
    if let role = axNonEmpty(descriptor.role) {
        if let label = axNonEmpty(descriptor.label) {
            text = role + " \u{00B7} \u{201C}" + label + "\u{201D}"
        } else if let window = axNonEmpty(descriptor.window) {
            text = role + " \u{00B7} " + window
        } else if let app = axNonEmpty(descriptor.app) {
            text = role + " \u{00B7} " + app
        } else {
            text = role
        }
    } else if let label = axNonEmpty(descriptor.label) {
        text = "\u{201C}" + label + "\u{201D}"
    } else {
        text = axNonEmpty(descriptor.window) ?? axNonEmpty(descriptor.app) ?? "\u{2026}"
    }
    return inspectTargetText(text)
}

/// A screen-sized click catcher. It must never take key or main status: the
/// user's app keeps keyboard focus while the mode is up. Mouse delivery does
/// not depend on key status, and `acceptsFirstMouse` makes the first click
/// count; `.nonactivatingPanel` means the click never activates this helper.
private final class InspectOverlayWindow: NSPanel {
    override var canBecomeKey: Bool {
        return false
    }

    override var canBecomeMain: Bool {
        return false
    }
}

/// The click-catching surface: a 5% black wash, a crosshair cursor, and one
/// badge that follows the pointer with the control under it.
///
/// The badge never covers the point under the pointer (the placement helper
/// flips it around the cursor and clamps it inside the screen), so the hover
/// preview can read exactly that point. The mode captures POINTER clicks only:
/// no key, no focus, no first responder.
private final class InspectOverlayView: NSView {
    private static let titleFont = NSFont.systemFont(ofSize: InspectModeSpec.badgeFontSize,
                                                     weight: .semibold)
    private static let detailFont = NSFont.systemFont(ofSize: InspectModeSpec.badgeFontSize)
    private static let titleAttributes: [NSAttributedString.Key: Any] = [
        .font: titleFont,
        .foregroundColor: NSColor.white
    ]
    private static let detailAttributes: [NSAttributedString.Key: Any] = [
        .font: detailFont,
        .foregroundColor: NSColor.white.withAlphaComponent(0.78)
    ]

    /// Where the badge is drawn, in this view's coordinates. Empty when the
    /// pointer is on another display (only one badge exists at a time).
    private(set) var badgeFrame = NSRect.zero
    private var badgeDetail = ""
    /// The gesture line: "click to ask" for a right-click arm, "release to ask"
    /// for a drag arm. Set with every badge update.
    private var badgeTitle = InspectModeSpec.clickTitle
    private var badgeCursor: NSPoint?
    private var cursorTracking: NSTrackingArea?

    /// Left click on the overlay, reported in AppKit screen coordinates.
    var onInspectClick: ((NSPoint) -> Void)?
    /// Right click anywhere: the documented exit.
    var onCancel: (() -> Void)?
    /// A click inside the badge is a cancel, not a read.
    var onBadgeClick: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        return nil
    }

    /// Never a first responder: the mode captures pointer clicks only, so no
    /// key event is ever routed here.
    override var acceptsFirstResponder: Bool {
        return false
    }

    /// The very first click counts: the window never becomes key, so without
    /// this the click that activates the helper would be swallowed.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }

    /// The crosshair is the mode's cursor, set for the whole surface.
    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }

    /// `.activeAlways` is what makes the crosshair survive this helper being a
    /// background accessory: the default tracking only fires for the active app.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = cursorTracking {
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
        cursorTracking = area
    }

    override func cursorUpdate(with event: NSEvent) {
        NSCursor.crosshair.set()
    }

    override func mouseDown(with event: NSEvent) {
        let local = convert(event.locationInWindow, from: nil)
        if badgeFrame.contains(local) {
            onBadgeClick?()
            return
        }
        guard let window = window else { return }
        onInspectClick?(window.convertPoint(toScreen: event.locationInWindow))
    }

    override func rightMouseDown(with event: NSEvent) {
        onCancel?()
    }

    /// Two 11 pt lines plus air: the pill is small, the second line one glance.
    static func badgeSize(for detail: String, title: String) -> NSSize {
        let titleWidth = (title as NSString)
            .size(withAttributes: [.font: titleFont]).width
        let detailLine = detail.isEmpty ? "\u{2026}" : detail
        let detailWidth = (detailLine as NSString)
            .size(withAttributes: [.font: detailFont]).width
        let width = min(max(titleWidth, detailWidth) + InspectModeSpec.badgeInsetX * 2.0,
                        InspectModeSpec.badgeMaxWidth)
        let height = InspectModeSpec.badgeInsetY * 2.0
            + InspectModeSpec.badgeLineHeight * 2.0
            + InspectModeSpec.badgeLineGap
        return NSSize(width: ceil(max(width, InspectModeSpec.badgeMinWidth)),
                      height: ceil(height))
    }

    /// Centred at (cursor + 18, cursor + 26) in window coordinates, clamped
    /// inside the screen. Four candidate centres are tried so a clamp at an
    /// edge can never leave the pill on the point under test.
    private func badgeFrame(cursor: NSPoint, size: NSSize) -> NSRect {
        func clamped(center: NSPoint) -> NSRect {
            var frame = NSRect(x: center.x - size.width / 2.0,
                               y: center.y - size.height / 2.0,
                               width: size.width,
                               height: size.height)
            if bounds.width > size.width {
                frame.origin.x = min(max(frame.origin.x, bounds.minX + 4.0),
                                     bounds.maxX - size.width - 4.0)
            } else {
                frame.origin.x = bounds.midX - size.width / 2.0
            }
            if bounds.height > size.height {
                frame.origin.y = min(max(frame.origin.y, bounds.minY + 4.0),
                                     bounds.maxY - size.height - 4.0)
            } else {
                frame.origin.y = bounds.midY - size.height / 2.0
            }
            return frame
        }
        let offsets = [(InspectModeSpec.badgeOffsetX, InspectModeSpec.badgeOffsetY),
                       (-InspectModeSpec.badgeOffsetX, -InspectModeSpec.badgeOffsetY),
                       (InspectModeSpec.badgeOffsetX, -InspectModeSpec.badgeOffsetY),
                       (-InspectModeSpec.badgeOffsetX, InspectModeSpec.badgeOffsetY)]
        for (dx, dy) in offsets {
            let frame = clamped(center: NSPoint(x: cursor.x + dx, y: cursor.y + dy))
            if !frame.contains(cursor) {
                return frame
            }
        }
        // Every centred candidate was clamped onto the point (a corner of the
        // screen). Pin the pill entirely to one SIDE of the cursor instead: the
        // x range alone then keeps the tested point clear.
        let y = min(max(cursor.y + InspectModeSpec.badgeOffsetY - size.height / 2.0,
                        bounds.minY + 4.0),
                    bounds.maxY - size.height - 4.0)
        var sideFrames: [NSRect] = []
        if cursor.x + InspectModeSpec.badgeOffsetX + size.width <= bounds.maxX - 4.0 {
            sideFrames.append(NSRect(x: cursor.x + InspectModeSpec.badgeOffsetX,
                                     y: y,
                                     width: size.width,
                                     height: size.height))
        }
        let leftX = max(cursor.x - InspectModeSpec.badgeOffsetX - size.width,
                        bounds.minX + 4.0)
        sideFrames.append(NSRect(x: leftX, y: y,
                                 width: size.width, height: size.height))
        for frame in sideFrames where !frame.contains(cursor) {
            return frame
        }
        // A panel narrower than the pill cannot clear it horizontally: keep the
        // pill entirely above (or below) the cursor so the Y range alone clears
        // the tested point.
        let clearX = min(max(cursor.x + InspectModeSpec.badgeOffsetX - size.width / 2.0,
                             bounds.minX + 4.0),
                         bounds.maxX - size.width - 4.0)
        if cursor.y + InspectModeSpec.badgeOffsetY + size.height <= bounds.maxY - 4.0 {
            return NSRect(x: clearX,
                          y: cursor.y + InspectModeSpec.badgeOffsetY,
                          width: size.width,
                          height: size.height)
        }
        let belowY = cursor.y - InspectModeSpec.badgeOffsetY - size.height
        if belowY >= bounds.minY + 4.0 {
            return NSRect(x: clearX, y: belowY,
                          width: size.width, height: size.height)
        }
        return sideFrames.first ?? NSRect(x: clearX, y: y,
                                          width: size.width, height: size.height)
    }

    /// The controller feeds the pointer (in this view's coordinates), the
    /// current target line and the gesture line on every tick. A nil cursor
    /// hides the badge. The title has no default on purpose: every arm mode
    /// must say how it resolves.
    func updateBadge(cursor: NSPoint?,
                     detail: String,
                     title: String) {
        let text = inspectTargetText(detail)
        guard cursor != badgeCursor || text != badgeDetail
            || title != badgeTitle else { return }
        badgeCursor = cursor
        badgeDetail = text
        badgeTitle = title
        if let cursor = cursor {
            badgeFrame = badgeFrame(cursor: cursor,
                                    size: InspectOverlayView.badgeSize(for: text,
                                                                       title: title))
        } else {
            badgeFrame = .zero
        }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard !badgeFrame.isEmpty else { return }

        let path = NSBezierPath(roundedRect: badgeFrame,
                                xRadius: badgeFrame.height / 2.0,
                                yRadius: badgeFrame.height / 2.0)
        NSColor(red: 22.0 / 255.0,
                green: 23.0 / 255.0,
                blue: 26.0 / 255.0,
                alpha: 0.92).setFill()
        path.fill()
        NSColor(red: 122.0 / 255.0,
                green: 134.0 / 255.0,
                blue: 255.0 / 255.0,
                alpha: 0.55).setStroke()
        path.lineWidth = 1.0
        path.stroke()

        let content = badgeFrame.insetBy(dx: InspectModeSpec.badgeInsetX,
                                         dy: InspectModeSpec.badgeInsetY)
        let titleRect = NSRect(x: content.minX,
                               y: content.maxY - InspectModeSpec.badgeLineHeight,
                               width: content.width,
                               height: InspectModeSpec.badgeLineHeight)
        (badgeTitle as NSString).draw(
            in: titleRect,
            withAttributes: InspectOverlayView.titleAttributes)
        let detail = badgeDetail.isEmpty ? "\u{2026}" : badgeDetail
        let detailRect = NSRect(x: content.minX,
                                y: titleRect.minY - InspectModeSpec.badgeLineGap
                                    - InspectModeSpec.badgeLineHeight,
                                width: content.width,
                                height: InspectModeSpec.badgeLineHeight)
        (detail as NSString).draw(in: detailRect,
                                  withAttributes: InspectOverlayView.detailAttributes)
    }
}

/// NSMenu needs an NSObject target/action pair and the controller is a plain
/// class, so each item's action lives in one of these boxes (held strong by the
/// controller for the life of the menu, since `NSMenuItem.target` is weak).
private final class HatMenuItemTarget: NSObject {
    private let handler: () -> Void

    init(_ handler: @escaping () -> Void) {
        self.handler = handler
    }

    @objc func fire(_ sender: Any?) {
        handler()
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
    /// Which anchor the hat is on right now, nil until the first placement of
    /// this arm. A hat already on the pet waits out the grace before going
    /// free; one that was never pet-anchored goes free at once.
    private var claimChipAnchorMode: ClaimChipAnchorMode?
    /// The mode last announced on stdout (`chip-anchor`), so one placement
    /// change is one line and a steady tick is silent.
    private var claimChipAnnouncedMode: ClaimChipAnchorMode?
    /// When the pet panel was first missed in the current pet-anchored run.
    private var claimChipPetMissingSince: Date?
    /// Remembered free-standing origin in AppKit coordinates (bottom-left
    /// origin), loaded lazily from the companion's preferences.
    private var claimChipFreeAnchorOrigin: NSPoint?
    /// A free-standing hat is being dragged to a new place: the same press
    /// moves the window instead of arming INSPECT MODE.
    private var claimChipFreeMoveActive = false
    /// True between "drag begins" and mouseUp: the placement timer must not
    /// snap the hat out from under the cursor while the user is aiming.
    private var claimChipDragging = false
    private var claimChipDragOffset = NSPoint.zero
    /// The pointer read-out panel and every way it can disappear.
    private var lookCardWindow: NSPanel?
    private var lookCardTimer: Timer?
    private var lookCardMonitors: [Any] = []
    private var lookCardGeneration = 0
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
    /// Set by the retained typed LOOK path just before `openSummonField()`
    /// raises the same box in LOOK mode; consumed and cleared by that call.
    private var pendingLookContext: String?
    private var pendingLookTarget: LookTarget?
    /// What the last drag read out. A hat click within `lookAskWindow` asks
    /// about THAT element instead of starting a blank request, so the gesture
    /// is "drag to see, click to ask about it".
    private var lastLookDescriptor: LookDescriptor?
    private var lastLookAt: Date?
    private let lookAskWindow: TimeInterval = 90.0
    /// The element the open field is asking about, or nil for the plain door.
    private var summonFieldLookTarget: LookTarget?

    // MARK: INSPECT MODE state
    //
    // Right-clicking the hat raises a screen-sized click catcher on every
    // display and hides the hat. The click the user means to make on a control
    // lands on US, so the control never fires; we read the element under that
    // point and ask about it. The hat is only an indicator while the mode is up
    // — a follower with an offset can never itself be clicked.
    private var inspectMode = false
    /// Armed by a hat DRAG instead of a right-click: the same mode, but the
    /// target is confirmed by the pointer's RELEASE, never by a press.
    private var inspectReleaseToAsk = false
    private var inspectWindows: [NSPanel] = []
    private var inspectViews: [InspectOverlayView] = []
    private var inspectTickTimer: Timer?
    private var inspectTimeoutTimer: Timer?
    /// Esc (local + global) and, while drag-armed, the release catcher. They
    /// are installed at arm and removed at disarm so no monitor outlives the
    /// mode.
    private var inspectMonitors: [Any] = []
    /// Where the mode was armed: a click that never left that point tests the
    /// arming point, never a stale pre-arm read-out.
    private var inspectArmPoint: CGPoint?
    /// Last point the hover preview hit-tested, and the badge's second line.
    private var inspectLastTestedPoint: CGPoint?
    private var inspectBadgeDetail = ""
    /// A click is being resolved: the overlays are parked and the tick stops.
    private var inspectResolving = false
    /// NSMenu action boxes must outlive the tracking loop.
    private var hatMenuTargets: [HatMenuItemTarget] = []
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
    // MARK: Ambient watch state
    //
    // v1 runs ONE passive watch at a time. The observer lives on the watched
    // app element (lifecycle), on every top-level window (title / destroyed /
    // moved) and on the focused control (value / title / selection). Every
    // callback is turned into one JSON line on stdout; nothing here clicks,
    // types or brings an app forward.
    /// Same C trampoline pattern as `petObserver`: one observer per watched pid.
    private var watchObserver: AXObserver?
    /// The exact app element the notifications were installed on. Kept so the
    /// app-quit check compares the SAME wrapper (never a freshly fabricated
    /// one) and so removal uses the element that was registered.
    private var watchRootElement: AXUIElement?
    /// Echoed on every line this watch emits; the producer ignores lines whose
    /// token does not match the watch it currently has installed.
    private var watchToken: String?
    private var watchedPid: pid_t?
    private var watchAppName: String?
    /// The condition to announce once ("" is never stored; nil means none).
    private var watchUntil: String?
    /// False after the condition fired, so one hit is announced per arm.
    private var watchHitArmed = false
    private var watchSeq = 0
    private var watchEventCount = 0
    /// CFHash -> window element, so stop can remove each window's notifications.
    private var observedWatchWindows: [Int: AXUIElement] = [:]
    /// Titles read at attach time, so a destroyed window can still be named.
    private var watchWindowTitles: [Int: String] = [:]
    /// The focused control. Re-installed and re-seeded on every focus change.
    private var watchFocusedElement: AXUIElement?
    /// Last readable string value per observed element (CFHash key), so a
    /// value event can carry old/new. Bounded by the controls actually focused.
    private var watchLastValues: [String: String] = [:]
    /// Event coalescer: at most `watchMaxEventsPerSecond` lines per second.
    private var watchEmitTimes: [Date] = []
    /// Duplicate gate: same signature within `watchDuplicateWindow` is dropped.
    private var watchLastSignatureAt: [String: Date] = [:]
    /// Required clean stop path: a watch must not survive the watched app.
    private var watchLivenessTimer: Timer?
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
        case .watchStart(let spec):
            cancelPendingExit()
            startWatch(spec)
        case .watchStop:
            stopWatch(reason: "stopped", emit: true)
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
            // Disarming first: INSPECT MODE holds timers and screen-sized
            // overlays that a closed hat must not leave behind.
            disarmInspectMode()
            claimChipTimer?.invalidate()
            claimChipTimer = nil
            claimChipTimerInterval = 0.0
            stopPetObservation()
            // Explicit disarm (this is the ONLY reason the hat goes away).
            // The next arm starts from a cold anchor again, so a fresh
            // companion with no pop-out shows the hat at the free point
            // immediately instead of waiting out the pet grace.
            claimChipAnchorMode = nil
            claimChipAnnouncedMode = nil
            claimChipPetMissingSince = nil
            claimChipFreeMoveActive = false
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

    // MARK: Ambient WATCH

    /// Resolve a running app by localized name: exact, then a UNIQUE prefix,
    /// then a unique substring. Ambiguity resolves to nothing: watching a
    /// guessed app is worse than asking the agent to retry. Regular (Dock)
    /// apps win ties over background helpers with similar names.
    private func findWatchApp(named name: String) -> NSRunningApplication? {
        let needle = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else {
            return nil
        }
        let ownPid = ProcessInfo.processInfo.processIdentifier
        let candidates = NSWorkspace.shared.runningApplications.filter { app in
            guard !app.isTerminated, app.processIdentifier != ownPid,
                  let localized = app.localizedName, !localized.isEmpty else {
                return false
            }
            return true
        }
        func nameLower(_ app: NSRunningApplication) -> String {
            return (app.localizedName ?? "").lowercased()
        }
        func uniquePick(_ matches: [NSRunningApplication]) -> NSRunningApplication? {
            if matches.count == 1 {
                return matches[0]
            }
            let regular = matches.filter { $0.activationPolicy == .regular }
            return regular.count == 1 ? regular[0] : nil
        }
        let exact = candidates.filter { nameLower($0) == needle }
        if let pick = uniquePick(exact) {
            return pick
        }
        let prefixed = candidates.filter { nameLower($0).hasPrefix(needle) }
        if let pick = uniquePick(prefixed) {
            return pick
        }
        let containing = candidates.filter { nameLower($0).contains(needle) }
        return uniquePick(containing)
    }

    /// Start the single active watch: resolve, install, reply on the same line
    /// protocol. A failed resolution answers `{"watch_error": ...}` instead of
    /// emitting events, so the caller never has to guess.
    private func startWatch(_ spec: WatchStartSpec) {
        guard !isShuttingDown else {
            return
        }
        guard AXIsProcessTrusted() else {
            writeWatchLine(watchFailure("app-not-trusted", token: spec.token))
            return
        }
        guard let app = findWatchApp(named: spec.app) else {
            writeWatchLine(watchFailure("app-not-running", token: spec.token))
            return
        }
        let pid = app.processIdentifier
        func failureCode() -> String {
            // The contract names only two failures. Trust was already true at
            // the top, so a create/attach refusal points at the app, not TCC;
            // report not-trusted only if trust itself changed under us.
            return AXIsProcessTrusted() ? "app-not-running" : "app-not-trusted"
        }

        // Build the NEW observer and its app-element notifications BEFORE
        // touching the running watch: a rejected start (app gone, AX refusing
        // the source) must leave the previous watch exactly as it was.
        var observer: AXObserver?
        let created = AXObserverCreate(pid, watchAXObserverCallback, &observer)
        guard created == .success, let newObserver = observer else {
            writeWatchLine(watchFailure(failureCode(), token: spec.token))
            return
        }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        let root = AXUIElementCreateApplication(pid)
        var registered = 0
        for name in [kAXFocusedUIElementChangedNotification,
                     kAXWindowCreatedNotification,
                     kAXWindowMiniaturizedNotification,
                     kAXWindowDeminiaturizedNotification,
                     kAXApplicationActivatedNotification,
                     kAXApplicationDeactivatedNotification,
                     kAXUIElementDestroyedNotification] {
            let error = AXObserverAddNotification(newObserver, root, name as CFString, refcon)
            if error == .success || error == .notificationAlreadyRegistered {
                registered += 1
            }
        }
        guard registered > 0 else {
            // Nothing could be installed: the app element is unusable, so this
            // is a failed START, not a live watch with a dead observer.
            writeWatchLine(watchFailure(failureCode(), token: spec.token))
            return
        }

        // Only now replace the running watch. Replacement is silent on purpose:
        // the producer asked for the new watch on the same pipe and owns the
        // replacement bookkeeping; a stray stop line would land on the NEW row.
        stopWatch(reason: "replaced", emit: false)

        watchAppName = app.localizedName ?? spec.app
        watchedPid = pid
        watchUntil = spec.until
        watchHitArmed = spec.until != nil
        watchSeq = 0
        watchEventCount = 0
        watchRootElement = root
        watchToken = spec.token
        watchEmitTimes.removeAll()
        watchLastSignatureAt.removeAll()
        watchLastValues.removeAll()
        watchWindowTitles.removeAll()
        watchObserver = newObserver

        CFRunLoopAddSource(CFRunLoopGetCurrent(),
                           AXObserverGetRunLoopSource(newObserver),
                           .commonModes)
        attachWatchWindowObservers()
        attachWatchFocusedObservers()
        startWatchLivenessTimer()
        var started: [String: Any] = [
            "op": "watch-started",
            "app": watchAppName ?? spec.app,
            "until": spec.until ?? NSNull(),
            "pid": Int(pid),
        ]
        if let token = spec.token {
            started["token"] = token
        }
        writeWatchLine(started)
        // Baseline: describe where the app is RIGHT NOW. A watch opened on a
        // condition that is already true must fire immediately instead of
        // waiting for the user to move something.
        let baseline = axElement(from: copyAXAttribute(root, "AXFocusedUIElement"))
            ?? axElement(from: copyAXAttribute(root, "AXFocusedWindow"))
        if let element = baseline {
            emitWatchEvent(kind: "focus",
                           role: watchRole(element),
                           label: watchLabel(element),
                           window: watchWindowLine(element))
        } else {
            emitWatchEvent(kind: "focus", role: "AXApplication",
                           label: watchAppName, window: watchFocusedWindowTitle())
        }
    }

    /// Clear every in-memory watch field without emitting or touching the
    /// observer (the observer teardown lives in `stopWatch`).
    private func resetWatchState() {
        watchedPid = nil
        watchRootElement = nil
        watchToken = nil
        watchAppName = nil
        watchUntil = nil
        watchHitArmed = false
        watchSeq = 0
        watchEventCount = 0
        observedWatchWindows.removeAll()
        watchWindowTitles.removeAll()
        watchFocusedElement = nil
        watchLastValues.removeAll()
        watchEmitTimes.removeAll()
        watchLastSignatureAt.removeAll()
        watchLivenessTimer?.invalidate()
        watchLivenessTimer = nil
    }

    /// Remove every observer, notification and run-loop source the watch owns.
    /// `watch_stop` emits `{"op":"watch-stopped","events":N}`; an app quit adds
    /// `"reason":"app-quit"`.
    private func stopWatch(reason: String, emit: Bool) {
        let wasWatching = watchObserver != nil || watchedPid != nil
        let events = watchEventCount
        let token = watchToken
        if let observer = watchObserver, let root = watchRootElement {
            for name in [kAXFocusedUIElementChangedNotification,
                         kAXWindowCreatedNotification,
                         kAXWindowMiniaturizedNotification,
                         kAXWindowDeminiaturizedNotification,
                         kAXApplicationActivatedNotification,
                         kAXApplicationDeactivatedNotification,
                         kAXUIElementDestroyedNotification] {
                _ = AXObserverRemoveNotification(observer, root, name as CFString)
            }
        }
        if let observer = watchObserver {
            // (focused and window removals below need the observer only)
            if let focused = watchFocusedElement {
                for name in [kAXValueChangedNotification,
                             kAXTitleChangedNotification,
                             kAXSelectedChildrenChangedNotification] {
                    _ = AXObserverRemoveNotification(observer, focused, name as CFString)
                }
            }
            for (_, window) in observedWatchWindows {
                for name in [kAXTitleChangedNotification,
                             kAXUIElementDestroyedNotification,
                             kAXWindowMovedNotification] {
                    _ = AXObserverRemoveNotification(observer, window, name as CFString)
                }
            }
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(),
                                  AXObserverGetRunLoopSource(observer),
                                  .commonModes)
        }
        watchObserver = nil
        resetWatchState()
        if emit {
            var payload: [String: Any] = ["op": "watch-stopped", "events": events]
            // The brief's plain stop is `{"op":"watch-stopped","events":N}`;
            // only the involuntary stop explains itself.
            if wasWatching && reason != "stopped" {
                payload["reason"] = reason
            }
            if let token = token {
                payload["token"] = token
            }
            writeWatchLine(payload)
        }
    }

    /// A watch must not outlive the app it observes. AXUIElementDestroyed on
    /// the app element covers the graceful path; this 2 s liveness tick covers
    /// a crash/kill, where no notification is delivered at all.
    private func startWatchLivenessTimer() {
        watchLivenessTimer?.invalidate()
        let timer = Timer(timeInterval: watchLivenessInterval, repeats: true) { [weak self] _ in
            guard let self = self, !self.isShuttingDown, let pid = self.watchedPid else {
                return
            }
            let app = NSRunningApplication(processIdentifier: pid)
            if app == nil || app?.isTerminated == true {
                self.stopWatch(reason: "app-quit", emit: true)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        watchLivenessTimer = timer
    }

    /// Observe every top-level window: title changes, destruction and moves.
    /// Moves are installed (the contract names them) but dropped in the handler:
    /// a drag streams them ~17 ms apart and they are not worth logging.
    private func attachWatchWindowObservers() {
        guard let observer = watchObserver, let pid = watchedPid else {
            return
        }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        let root = AXUIElementCreateApplication(pid)
        let windows = axWindowElements(of: root)
        // Titles for windows that are no longer listed can never name a future
        // event; keep the table bounded on a long watch.
        let liveWindowKeys = Set(windows.map { Int(CFHash($0)) })
        watchWindowTitles = watchWindowTitles.filter {
            liveWindowKeys.contains($0.key)
        }
        for window in windows {
            let key = Int(CFHash(window))
            if let title = axNonEmpty(axWindowTitle(window)) {
                watchWindowTitles[key] = title
            }
            guard observedWatchWindows[key] == nil else {
                continue
            }
            var registered = false
            for name in [kAXTitleChangedNotification,
                         kAXUIElementDestroyedNotification,
                         kAXWindowMovedNotification] {
                let error = AXObserverAddNotification(observer, window, name as CFString, refcon)
                if error == .success || error == .notificationAlreadyRegistered {
                    registered = true
                }
            }
            if registered {
                observedWatchWindows[key] = window
            }
        }
    }

    /// Install value/title/selection observers on the FOCUSED control. Called
    /// once at start and again on every AXFocusedUIElementChanged, moving the
    /// notifications off the previous control so they don't pile up.
    private func attachWatchFocusedObservers() {
        guard let observer = watchObserver, let pid = watchedPid else {
            return
        }
        let root = AXUIElementCreateApplication(pid)
        guard let focused = axElement(from: copyAXAttribute(root, "AXFocusedUIElement")) else {
            watchFocusedElement = nil
            return
        }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        let key = Int(CFHash(focused))
        if let previous = watchFocusedElement, Int(CFHash(previous)) != key {
            for name in [kAXValueChangedNotification,
                         kAXTitleChangedNotification,
                         kAXSelectedChildrenChangedNotification] {
                _ = AXObserverRemoveNotification(observer, previous, name as CFString)
            }
        }
        watchFocusedElement = focused
        // Only the current control's baselines are reachable: value/title/
        // selection observers moved off the old one, so drop its entries rather
        // than growing a dictionary for the life of a long watch.
        let liveKeys: Set<String> = [String(key), "t:" + String(key),
                                     "sel:" + String(key)]
        watchLastValues = watchLastValues.filter { liveKeys.contains($0.key) }
        for name in [kAXValueChangedNotification,
                     kAXTitleChangedNotification,
                     kAXSelectedChildrenChangedNotification] {
            _ = AXObserverAddNotification(observer, focused, name as CFString, refcon)
        }
        // Seed the value baseline: the first AXValueChanged on this control can
        // then report a readable old value instead of only the new one.
        if watchLastValues[String(key)] == nil, let value = axWatchRawValueText(focused) {
            watchLastValues[String(key)] = value
        }
    }

    /// Entry point from the C callback (mirrors petAXNotificationFired).
    fileprivate func watchAXNotificationFired(_ name: String, element: AXUIElement) {
        DispatchQueue.main.async { [weak self] in
            self?.processWatchAXNotification(name, element: element)
        }
    }

    private func processWatchAXNotification(_ name: String, element: AXUIElement) {
        guard watchObserver != nil, !isShuttingDown else {
            return
        }
        // AXWindowMoved is the hot path of a drag: drop it before any AX read.
        if name == kAXWindowMovedNotification {
            return
        }
        // Cheap liveness gate on every event: a killed app delivers nothing.
        if let pid = watchedPid {
            let app = NSRunningApplication(processIdentifier: pid)
            if app == nil || app?.isTerminated == true {
                stopWatch(reason: "app-quit", emit: true)
                return
            }
        }
        switch name {
        case kAXUIElementDestroyedNotification:
            let key = Int(CFHash(element))
            if let root = watchRootElement, CFEqual(element, root) {
                stopWatch(reason: "app-quit", emit: true)
                return
            }
            // Name it from the attach-time title: a destroyed element no longer
            // answers AXTitle.
            let title = watchWindowTitles[key] ?? axNonEmpty(axWindowTitle(element))
            observedWatchWindows.removeValue(forKey: key)
            emitWatchEvent(kind: "window-gone", role: "AXWindow", label: title,
                           window: title)
            attachWatchWindowObservers()
        case kAXFocusedUIElementChangedNotification:
            attachWatchFocusedObservers()
            if let focused = watchFocusedElement {
                emitWatchEvent(kind: "focus", role: watchRole(focused),
                               label: watchLabel(focused),
                               window: watchWindowLine(focused))
            } else {
                emitWatchEvent(kind: "focus", role: "AXApplication",
                               label: watchAppName, window: watchFocusedWindowTitle())
            }
        case kAXWindowCreatedNotification:
            // Resolve the new window BEFORE attaching: after the attach it is
            // just another known window, and AXFocusedWindow can still point at
            // the previous one this early.
            let newWindow = watchUnobservedWindowElement()
            attachWatchWindowObservers()
            let title = newWindow.flatMap { axNonEmpty(axWindowTitle($0)) }
                ?? watchFocusedWindowTitle()
            emitWatchEvent(kind: "window-new", role: "AXWindow", label: title,
                           window: title)
        case kAXWindowMiniaturizedNotification, kAXWindowDeminiaturizedNotification:
            // Collapsing a window hides it (window-gone); restoring shows it.
            let title = axNonEmpty(axWindowTitle(element))
                ?? watchWindowTitles[Int(CFHash(element))]
            let kind = name == kAXWindowMiniaturizedNotification
                ? "window-gone" : "window-new"
            emitWatchEvent(kind: kind, role: "AXWindow", label: title, window: title)
        case kAXTitleChangedNotification:
            let key = Int(CFHash(element))
            let role = watchRole(element)
            let title = axNonEmpty(axWindowTitle(element))
            if let title = title {
                watchWindowTitles[key] = title
            }
            if role == "AXWindow" {
                let windowTitle = title ?? watchWindowTitles[key]
                emitWatchEvent(kind: "window-title", role: role,
                               label: windowTitle, window: windowTitle)
            } else {
                // A control's title change is content. The contract has no
                // separate control-title kind, so it rides "value" (with the
                // old title when we saw it).
                let newValue = axWatchRawValueText(element) ?? title ?? ""
                let stored = watchLastValues["t:" + String(key)]
                watchLastValues["t:" + String(key)] = newValue
                emitWatchEvent(kind: "value", role: role, label: watchLabel(element),
                               window: watchWindowLine(element),
                               old: stored, new: newValue)
            }
        case kAXValueChangedNotification:
            let key = Int(CFHash(element))
            let newValue = axWatchRawValueText(element)
            let oldValue = watchLastValues[String(key)]
            if let newValue = newValue {
                watchLastValues[String(key)] = newValue
            }
            emitWatchEvent(kind: "value", role: watchRole(element),
                           label: watchLabel(element),
                           window: watchWindowLine(element),
                           old: oldValue, new: newValue)
        case kAXSelectedChildrenChangedNotification:
            let key = Int(CFHash(element))
            let newValue = watchSelectedText(element)
            let oldValue = watchLastValues["sel:" + String(key)]
            if let newValue = newValue {
                watchLastValues["sel:" + String(key)] = newValue
            }
            emitWatchEvent(kind: "value", role: watchRole(element),
                           label: watchLabel(element),
                           window: watchWindowLine(element),
                           old: oldValue, new: newValue)
        case kAXApplicationActivatedNotification:
            emitWatchEvent(kind: "activated", role: "AXApplication",
                           label: watchAppName, window: watchFocusedWindowTitle())
        case kAXApplicationDeactivatedNotification:
            emitWatchEvent(kind: "deactivated", role: "AXApplication",
                           label: watchAppName, window: watchFocusedWindowTitle())
        default:
            break
        }
    }

    private func watchRole(_ element: AXUIElement) -> String {
        return axNonEmpty(axString(element, "AXRole")) ?? "unknown"
    }

    /// AXTitle, else AXDescription, else AXValue — trimmed to 120 chars.
    private func watchLabel(_ element: AXUIElement) -> String? {
        if let title = axNonEmpty(axString(element, "AXTitle")) {
            return String(title.prefix(watchLabelLimit))
        }
        if let description = axNonEmpty(axString(element, "AXDescription")) {
            return String(description.prefix(watchLabelLimit))
        }
        return axWatchValueText(element)
    }

    /// AXValue as a readable string, trimmed. An empty string is still a
    /// readable value (a field being cleared is a real change); nil means the
    /// attribute is absent or not a string (numbers/ranges are not log-worthy).
    private func axWatchRawValueText(_ element: AXUIElement) -> String? {
        guard let raw = copyAXAttribute(element, "AXValue"),
              let text = raw as? String else {
            return nil
        }
        return String(text.trimmingCharacters(in: .whitespacesAndNewlines)
            .prefix(watchLabelLimit))
    }

    /// The value only counts when it is a readable NON-EMPTY string: a label
    /// should never collapse to "". Value events use axWatchRawValueText.
    private func axWatchValueText(_ element: AXUIElement) -> String? {
        guard let text = axWatchRawValueText(element), !text.isEmpty else {
            return nil
        }
        return text
    }

    /// The first top-level window not attached yet: on AXWindowCreated that is
    /// the window that just appeared.
    private func watchUnobservedWindowElement() -> AXUIElement? {
        guard let pid = watchedPid else {
            return nil
        }
        let root = AXUIElementCreateApplication(pid)
        for window in axWindowElements(of: root)
        where observedWatchWindows[Int(CFHash(window))] == nil {
            return window
        }
        // A sheet/panel that AXWindows does not list: say "a window appeared"
        // with no title rather than re-reporting an already-known window.
        return nil
    }

    private func watchFocusedWindowElement() -> AXUIElement? {
        guard let pid = watchedPid else {
            return nil
        }
        let root = AXUIElementCreateApplication(pid)
        if let window = axElement(from: copyAXAttribute(root, "AXFocusedWindow")) {
            return window
        }
        return axWindowElements(of: root).first
    }

    private func watchFocusedWindowTitle() -> String? {
        guard let window = watchFocusedWindowElement() else {
            return nil
        }
        return axNonEmpty(axWindowTitle(window))
    }

    /// The window line for a control: its enclosing AXWindow title, falling
    /// back to the app's focused window (Electron nests deeper than the 4-level
    /// ancestor walk in `axEnclosingWindow`).
    private func watchWindowLine(_ element: AXUIElement) -> String? {
        if let title = axEnclosingWindow(element).window {
            return title
        }
        return watchFocusedWindowTitle()
    }

    private func watchSelectedText(_ element: AXUIElement) -> String? {
        if let text = axNonEmpty(axString(element, "AXSelectedText")) {
            return String(text.prefix(watchLabelLimit))
        }
        guard let first = axElementList(element, attribute: "AXSelectedChildren").first else {
            return nil
        }
        return watchLabel(first) ?? axWatchValueText(first)
    }

    /// Emit one watch event through the coalescer and check the condition.
    ///
    /// The condition runs BEFORE the 4/s + 150 ms gates: a genuine
    /// "Export complete" must never be dropped because the app was noisy in the
    /// same second. The hit is emitted once per arm (`watchHitArmed`), then the
    /// watch keeps observing but stops matching until a new watch_start.
    private func emitWatchEvent(kind: String, role: String?, label: String?,
                                window: String?, old: String? = nil, new: String? = nil) {
        guard watchObserver != nil, !isShuttingDown else {
            return
        }
        let now = Date()

        if watchHitArmed, let until = watchUntil, !until.isEmpty {
            let needle = until.lowercased()
            let labelText = (label ?? "").lowercased()
            let valueText = (new ?? "").lowercased()
            // A window title counts as a match: "tell me when Downloads shows
            // up" is the window title, not a label on any control inside it.
            // Measured miss: watch("Finder", until: "Downloads") sat at
            // "no hit" while every event carried window "Downloads".
            let windowText = (window ?? "").lowercased()
            if labelText.contains(needle) || valueText.contains(needle)
                || windowText.contains(needle) {
                watchHitArmed = false
                var hit: [String: Any] = [
                    "op": "watch-hit",
                    "app": watchAppName ?? "",
                    "until": until,
                    "label": label ?? "",
                    "window": window ?? "",
                    "at": now.timeIntervalSince1970,
                ]
                if let token = watchToken {
                    hit["token"] = token
                }
                writeWatchLine(hit)
            }
        }

        watchEmitTimes.removeAll { now.timeIntervalSince($0) > watchRateWindow }
        guard watchEmitTimes.count < watchMaxEventsPerSecond else {
            return
        }
        let signature = [kind, role ?? "", label ?? "", window ?? "", new ?? ""]
            .joined(separator: "\u{1}")
        if let last = watchLastSignatureAt[signature],
           now.timeIntervalSince(last) < watchDuplicateWindow {
            return
        }
        watchLastSignatureAt[signature] = now
        // Keep the duplicate table bounded on a long watch.
        if watchLastSignatureAt.count > 32 {
            watchLastSignatureAt = watchLastSignatureAt.filter {
                now.timeIntervalSince($0.value) <= 5.0
            }
        }
        watchEmitTimes.append(now)
        watchSeq += 1
        watchEventCount += 1

        var payload: [String: Any] = [
            "op": "watch-event",
            "seq": watchSeq,
            "at": now.timeIntervalSince1970,
            "kind": kind,
            "app": watchAppName ?? "",
            "role": role ?? "unknown",
            "label": label ?? "",
            "window": window ?? "",
        ]
        if let token = watchToken {
            payload["token"] = token
        }
        // old/new when BOTH sides are readable strings (an empty string counts:
        // clearing a field is a change worth showing).
        if let old = old, let new = new {
            payload["old"] = old
            payload["new"] = new
        }
        writeWatchLine(payload)
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

    /// Place the hat from the best anchor available, in this order:
    ///
    ///   a. the popped-out pet panel, beside its sprite (unchanged);
    ///   b. the free anchor — a remembered screen point — used whenever no pet
    ///      panel has been found for `ClaimChipFreeAnchor.petGrace` seconds.
    ///
    /// The free anchor is the answer when neither resolves, so the hat (LOOK,
    /// the ask field, the watch menu) is reachable even in the fresh-install
    /// state where the pet is docked inside the Hermes window and no pet panel
    /// exists at all. Before this, `hermesPetWindowFrame` returned nil there and
    /// the chip was created and destroyed forever.
    private func positionClaimChip() {
        // The slow reconciliation tick also heals the attach set: a window
        // created while the observer was not running, or an element that went
        // stale across sleep/wake, gets observed again. No-op when the
        // observer is unavailable, so the fallback poll stays pure.
        attachPetWindowObservers()
        recoverLostPressIfNeeded()
        // The drag owns the frame until mouseUp; the timer must stay out of it.
        guard !claimChipDragging else { return }
        // INSPECT MODE hides the hat on purpose: the overlay owns the pointer,
        // so the placement tick must never bring the chip back mid-mode.
        guard !inspectMode else { return }
        guard claimChipEnabled, !isShuttingDown,
              let primaryHeight = primaryScreenHeight() else {
            // Explicit disarm (or shutdown, or no screen at all) is the only
            // correct reason to hide: an unresolved PET must never hide the
            // hat, because the free anchor is always underneath it.
            hideClaimChip()
            return
        }
        if let petWindow = hermesPetWindowFrame(primaryHeight: primaryHeight) {
            claimChipPetMissingSince = nil
            applyClaimChipFrame(petWindow: petWindow, primaryHeight: primaryHeight)
            return
        }
        // No pet panel found. A hat already anchored to the pet waits out the
        // grace so a pop-out being dragged, resized or created does not flicker
        // it to the corner; a hat that has never been pet-anchored (fresh
        // install, docked pet) goes free at once — the hat must never vanish.
        let now = Date()
        if claimChipAnchorMode == .pet {
            let missingSince = claimChipPetMissingSince ?? now
            claimChipPetMissingSince = missingSince
            if now.timeIntervalSince(missingSince) < ClaimChipFreeAnchor.petGrace {
                return
            }
        }
        applyFreeAnchorFrame()
    }

    /// Place (or move) the chip from a known pet-panel frame. `positionClaimChip`
    /// feeds it the polled anchor; the AX move/resize path feeds it the frame
    /// straight off the observed element, so a drag costs no enumeration.
    ///
    /// Both callers guarantee the frame is pet-shaped: the poll filters inside
    /// `hermesPetWindowFrame`, and the notification path filters with
    /// `isPetShapedFrame` before it schedules a placement (a docked pet means
    /// the Hermes main window is what moves, and that frame is dropped there).
    private func applyClaimChipFrame(petWindow: CGRect, primaryHeight: CGFloat) {
        recoverLostPressIfNeeded()
        guard !claimChipDragging else { return }
        // The overlay owns the frame while the mode is up.
        guard !inspectMode else { return }
        guard claimChipEnabled, !isShuttingDown else {
            // Explicit disarm/shutdown only: a missing anchor never hides the
            // hat, it moves it to the free point instead.
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
            // The pet is off-screen (another Space, minimised) or every
            // candidate overlaps the sprite: the hat goes free rather than
            // disappearing, so its doors stay one gesture away.
            applyFreeAnchorFrame()
            return
        }
        presentClaimChipFrame(appKitFrame: appKitRect(fromQuartz: frame,
                                                      primaryHeight: primaryHeight),
                              mode: .pet)
    }

    /// The free-standing hat: a remembered screen point the user can drag.
    /// With nothing remembered it sits at the main screen's bottom-right
    /// corner, 40 pt in from each edge (see `ClaimChipFreeAnchor`).
    private func applyFreeAnchorFrame() {
        recoverLostPressIfNeeded()
        guard !claimChipDragging, !inspectMode else { return }
        guard claimChipEnabled, !isShuttingDown else {
            hideClaimChip()
            return
        }
        let size = claimChipSize()
        guard let origin = freeHatOrigin(size: size) else {
            // No screen geometry at all: leave the hat exactly where it is
            // rather than hiding a door the user may be looking at.
            return
        }
        presentClaimChipFrame(appKitFrame: NSRect(origin: origin, size: size),
                              mode: .free)
    }

    /// The remembered free anchor, clamped onto a visible screen so a display
    /// change can never strand the hat off the desktop. The clamp is not
    /// persisted: only the user's own drag rewrites the remembered point.
    private func freeHatOrigin(size: CGSize) -> NSPoint? {
        let screens = NSScreen.screens
        guard !screens.isEmpty else {
            return nil
        }
        let remembered = claimChipFreeAnchorOrigin
            ?? ClaimChipFreeAnchor.remembered(from: UserDefaults.standard)
        let preferred: NSPoint
        if let remembered = remembered {
            preferred = remembered
        } else if let fallback = ClaimChipFreeAnchor.defaultOrigin(size: size) {
            preferred = fallback
        } else {
            return nil
        }
        claimChipFreeAnchorOrigin = preferred
        return clampHatOrigin(preferred, size: size)
    }

    /// Keep the free hat fully inside the visible frame of the screen it is on,
    /// by the same 4 pt margin the pet-relative placement uses. Without this a
    /// display change (or a drag to the edge) could park the hat off the
    /// desktop and make its doors unreachable again.
    private func clampHatOrigin(_ origin: NSPoint, size: CGSize) -> NSPoint? {
        let screens = NSScreen.screens
        guard !screens.isEmpty else {
            return nil
        }
        let probe = NSRect(x: origin.x, y: origin.y,
                           width: size.width, height: size.height)
        let host = screens.first { $0.frame.intersects(probe) }
            ?? primaryScreen()
            ?? screens[0]
        let visible = host.visibleFrame
        let margin = ClaimChipFreeAnchor.edgeMargin
        return NSPoint(x: min(max(origin.x, visible.minX + margin),
                              visible.maxX - size.width - margin),
                       y: min(max(origin.y, visible.minY + margin),
                              visible.maxY - size.height - margin))
    }

    /// Put the hat window at a resolved AppKit frame and report the anchor mode
    /// once per change. Both anchors land here, so the window's creation, its
    /// click wiring and the unchanged-frame guard exist exactly once.
    private func presentClaimChipFrame(appKitFrame: NSRect,
                                       mode: ClaimChipAnchorMode) {
        guard claimChipEnabled, !isShuttingDown else {
            return
        }
        claimChipAnchorMode = mode
        if mode == .pet {
            // Both placement paths land here, so the grace timer is cleared on
            // the event-driven path too: a pop-out that closes, reopens (AX
            // move/created) and closes again must not inherit the old miss and
            // expire the grace instantly.
            claimChipPetMissingSince = nil
        }
        announceClaimChipAnchor(mode)
        if let window = claimChipWindow, let view = claimChipView {
            // A free hat is moved by its drag, so the eye glyph would be a lie
            // ("I am reading the pointer"); it keeps the hat glyph instead.
            view.dragMovesTheHat = (mode == .free)
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
        view.dragMovesTheHat = (mode == .free)
        view.onClick = { [weak self] in
            self?.claimChipClicked()
        }
        view.onRightClick = { [weak self] point in
            self?.claimChipRightClicked(at: point)
        }
        view.onPressBegan = { [weak self] point in
            self?.beginChipPress(at: point)
        }
        view.onDragBegan = { [weak self] point in
            self?.beginLookDrag(at: point)
        }
        view.onDragMoved = { [weak self] point in
            self?.moveLookDrag(to: point)
        }
        view.onDragEnded = { [weak self] point in
            self?.endLookDrag(at: point)
        }
        window.contentView = view
        claimChipWindow = window
        claimChipView = view
        window.orderFrontRegardless()
    }

    /// One stdout line per placement CHANGE, so the Python side can report the
    /// state instead of inferring it: `mode` is `pet` while the hat sits beside
    /// the popped-out pet, `free` when it is the free-standing hat. A steady
    /// tick is silent, so this costs one line per real transition.
    private func announceClaimChipAnchor(_ mode: ClaimChipAnchorMode) {
        guard claimChipAnnouncedMode != mode else {
            return
        }
        claimChipAnnouncedMode = mode
        writeEventLine(["op": "chip-anchor", "mode": mode.rawValue])
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
        // The press latched placement off; a plain click is not a drag.
        claimChipDragging = false
        // Using the hat again puts any read-out away first.
        dismissLookCard()
        guard claimChipEnabled else {
            chipJournal("click-skipped:disabled")
            return
        }
        // Drag-to-look then click-to-ask: the element the user just dropped the
        // hat on is the subject of the request, so they never have to describe
        // what they are pointing at.
        if let descriptor = lastLookDescriptor,
           let at = lastLookAt,
           Date().timeIntervalSince(at) < lookAskWindow,
           !summonFieldOpen {
            lastLookDescriptor = nil
            lastLookAt = nil
            let target = LookTarget(app: descriptor.app,
                                    role: descriptor.role,
                                    label: descriptor.label,
                                    window: descriptor.window,
                                    doc: descriptor.doc,
                                    enabled: descriptor.enabled,
                                    box: descriptor.box)
            let line = target.contextLine
            pendingLookContext = line.isEmpty ? nil : line
            pendingLookTarget = line.isEmpty ? nil : target
            chipJournal("ask-about-look")
            openSummonField()
            return
        }
        toggleSummonField()
    }

    // MARK: Pointer LOOK gesture

    /// A press latches placement off until mouseUp. If the window was hidden
    /// or reordered mid-gesture the mouseUp never reaches the view; the
    /// physical button is up, so release the latch rather than pin the hat
    /// forever.
    private func recoverLostPressIfNeeded() {
        guard claimChipDragging, NSEvent.pressedMouseButtons == 0 else {
            return
        }
        claimChipDragging = false
        claimChipView?.cancelPendingPress()
        // A lost free-hat drag keeps the place it reached: the release is gone,
        // but the user's last position is the honest remembered point.
        if claimChipFreeMoveActive {
            endHatFreeMove()
        }
        chipJournal("look-press-recovered")
    }

    /// mouseDown latches placement off immediately. Without this the slow
    /// reconciliation tick could snap the hat between press and the first moved
    /// point, so the very first drag move would start from a shifted frame.
    private func beginChipPress(at screenPoint: NSPoint) {
        dismissLookCard()
        guard let window = claimChipWindow else { return }
        claimChipDragging = true
        claimChipDragOffset = NSPoint(x: window.frame.minX - screenPoint.x,
                                      y: window.frame.minY - screenPoint.y)
    }

    /// The press crossed `dragThreshold`: the grab is already latched.
    ///
    /// Pet-anchored, the drag arms the SAME INSPECT MODE a right-click arms,
    /// only with the release as the confirmation, so both gestures offer one
    /// read-out and one ask field. Free-standing, the same gesture MOVES the
    /// hat instead: there is no pet to read against, and a user's first instinct
    /// with a loose badge is to place it. INSPECT MODE is still one right-click
    /// away, and the plain click still opens the ask field.
    private func beginLookDrag(at screenPoint: NSPoint) {
        if !claimChipDragging, let window = claimChipWindow {
            claimChipDragging = true
            claimChipDragOffset = NSPoint(x: window.frame.minX - screenPoint.x,
                                          y: window.frame.minY - screenPoint.y)
        }
        if claimChipAnchorMode == .free {
            beginHatFreeMove()
            return
        }
        armInspectMode(releaseToAsk: true)
        // The arm can refuse (an ask field already open, the chip not live, no
        // screen): say so instead of failing silently.
        if !inspectMode {
            chipJournal("drag-arm-refused")
        }
    }

    /// Nothing to drag any more: INSPECT MODE owns the pointer and the hat is
    /// hidden for the duration, so moving the window here would move something
    /// nobody can see. The free hat has no such mode, so IT is what moves.
    private func moveLookDrag(to screenPoint: NSPoint) {
        if claimChipFreeMoveActive {
            hatFreeMove(to: screenPoint)
            return
        }
        guard !inspectMode, claimChipDragging, let window = claimChipWindow else {
            return
        }
        window.setFrameOrigin(NSPoint(x: screenPoint.x + claimChipDragOffset.x,
                                      y: screenPoint.y + claimChipDragOffset.y))
    }

    /// Release: a drag-armed mode confirms HERE. The release point is handed to
    /// the SAME resolver the click-armed mode uses (`resolveInspectClick`), so
    /// parking, the deferred AX read and the ask field are one path. Nothing
    /// here activates the app, focuses Hermes or types.
    private func endLookDrag(at screenPoint: NSPoint) {
        // The free move owns its own latch: ending it must not depend on the
        // drag latch, so a lost press can never leave it stuck on.
        if claimChipFreeMoveActive {
            // The free hat was being placed, not read: remember the drop. The
            // two latches are disjoint by construction (a free hat never arms
            // inspect), so clearing the drag latch here is belt-and-braces.
            claimChipDragging = false
            endHatFreeMove()
            return
        }
        guard claimChipDragging else { return }
        claimChipDragging = false
        // The mode may already be gone (Esc, timeout, a lost grab): disarm put
        // the hat back, so the release has nothing left to confirm.
        guard inspectMode, inspectReleaseToAsk else {
            chipJournal("drag-release:cancelled")
            return
        }
        resolveInspectRelease(at: screenPoint)
    }

    /// A free-standing hat is MOVED by the drag. This is deliberately NOT the
    /// inspect arm: a hat floating on its own has no pet to point at, and the
    /// user's ask is "put the butler where I can see it". Journaled so the mode
    /// is visible in `chip-events.log`.
    private func beginHatFreeMove() {
        claimChipFreeMoveActive = true
        chipJournal("hat-free-move")
    }

    /// Move the free hat with the pointer, using the offset captured at press so
    /// the hat never jumps under the cursor. Nothing is persisted here: a drag
    /// that ends in a cancel must not rewrite the remembered point.
    private func hatFreeMove(to screenPoint: NSPoint) {
        guard claimChipFreeMoveActive, !inspectMode,
              let window = claimChipWindow else { return }
        let raw = NSPoint(x: screenPoint.x + claimChipDragOffset.x,
                          y: screenPoint.y + claimChipDragOffset.y)
        guard raw.x.isFinite, raw.y.isFinite,
              let origin = clampHatOrigin(raw, size: claimChipSize()) else {
            return
        }
        window.setFrameOrigin(origin)
    }

    /// The release persisted the free hat: the window stays exactly where the
    /// user dropped it and that point becomes the remembered anchor, in the
    /// companion's own preferences, so it survives a restart. The window origin
    /// is authoritative: `hatFreeMove` already clamped it, so what is stored is
    /// exactly what the user sees.
    private func endHatFreeMove() {
        guard claimChipFreeMoveActive else { return }
        claimChipFreeMoveActive = false
        guard let window = claimChipWindow else { return }
        let origin = window.frame.origin
        claimChipFreeAnchorOrigin = origin
        ClaimChipFreeAnchor.remember(origin, in: UserDefaults.standard)
        chipJournal("hat-free-anchored")
    }

    /// Park the overlays, then resolve on the next run-loop turn — exactly the
    /// click path, triggered by a release. The local mouseUp monitor and the
    /// chip's own mouseUp can both land here; `inspectResolving` makes the
    /// second one a no-op.
    private func resolveInspectRelease(at screenPoint: NSPoint) {
        guard inspectMode, inspectReleaseToAsk, !inspectResolving,
              !isShuttingDown else { return }
        guard let primaryHeight = primaryScreenHeight(),
              screenPoint.x.isFinite, screenPoint.y.isFinite else {
            cancelInspectMode(reason: "no-point")
            return
        }
        let point = CGPoint(x: screenPoint.x, y: primaryHeight - screenPoint.y)
        inspectResolving = true
        inspectTickTimer?.invalidate()
        inspectTickTimer = nil
        inspectTimeoutTimer?.invalidate()
        inspectTimeoutTimer = nil
        // §8: our own windows win every AX hit-test, so every overlay is parked
        // off-screen (orderOut alone is applied by the window server on the
        // NEXT pass) and the read happens on the next run-loop turn.
        for window in inspectWindows {
            window.setFrameOrigin(NSPoint(x: -20000.0, y: -20000.0))
        }
        DispatchQueue.main.async { [weak self] in
            // `resolveInspectClick` re-checks `inspectMode` first, so an Esc (or
            // any other cancel) that lands in this one-turn gap wins and this
            // block becomes a no-op instead of opening the field.
            self?.resolveInspectClick(at: point, retries: 1)
        }
    }

    /// The release point when the chip's mouseUp is lost (the window is hidden
    /// for the duration, which is exactly when AppKit drops it): the global
    /// pointer position IS the release point at that instant.
    private func inspectReleasedAtMouseUp() {
        guard inspectMode, inspectReleaseToAsk, !inspectResolving else { return }
        resolveInspectRelease(at: NSEvent.mouseLocation)
    }

    /// DEAD SINCE THE DRAG PARITY: no caller — the drag arms INSPECT MODE now,
    /// so the read-out card is only reachable from this unreferenced resolver.
    /// Kept (with `presentLookCard` / `lookCardFrame`) instead of deleting a
    /// large Swift block hours before a submission; delete once the demo is over.
    private func resolveLookDrop(at point: CGPoint,
                                 primaryHeight: CGFloat,
                                 retries: Int) {
        var descriptor = lookElementDescriptor(at: point)
        if lookDescriptorIsSelf(descriptor), retries > 0 {
            // One short retry while the parked window settles; the hat stays
            // off-screen until this resolves.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
                self?.resolveLookDrop(at: point,
                                      primaryHeight: primaryHeight,
                                      retries: retries - 1)
            }
            return
        }
        if lookDescriptorIsSelf(descriptor) {
            descriptor = lookDescriptorForActiveApp()
            chipJournal("look-self-hit-fallback")
        } else {
            chipJournal("look-hit:" + (descriptor.app ?? "?")
                        + "/" + (descriptor.role ?? "?"))
        }
        positionClaimChip()
        claimChipWindow?.orderFrontRegardless()
        lastLookDescriptor = descriptor
        lastLookAt = Date()
        presentLookCard(descriptor, at: point, primaryHeight: primaryHeight)
    }

    /// Place the card next to the drop point, flipped/clamped so it never sits
    /// under the pointer and always stays on the screen that owns it.
    /// DEAD SINCE THE DRAG PARITY: only `presentLookCard` calls this now.
    private func lookCardFrame(at point: CGPoint,
                               size: CGSize,
                               primaryHeight: CGFloat) -> CGRect? {
        let anchor = CGRect(x: point.x, y: point.y, width: 1.0, height: 1.0)
        guard let screen = screenContaining(anchor,
                                            primaryHeight: primaryHeight) else {
            return nil
        }
        let visible = quartzRect(fromAppKit: screen.visibleFrame,
                                 primaryHeight: primaryHeight)
        let gap: CGFloat = 16.0
        var frame = CGRect(x: point.x + gap, y: point.y + gap,
                           width: size.width, height: size.height)
        if frame.maxX > visible.maxX - 4.0 {
            frame.origin.x = point.x - gap - size.width
        }
        if frame.maxY > visible.maxY - 4.0 {
            frame.origin.y = point.y - gap - size.height
        }
        frame.origin.x = min(max(frame.origin.x, visible.minX + 4.0),
                             visible.maxX - frame.width - 4.0)
        frame.origin.y = min(max(frame.origin.y, visible.minY + 4.0),
                             visible.maxY - frame.height - 4.0)
        return frame
    }

    /// DEAD SINCE THE DRAG PARITY: no caller. The drag arms INSPECT MODE and
    /// resolves through `resolveInspectClick`; the card below (and its class
    /// near the top of this file) is kept only until the demo is over, then
    /// deleted in one piece.
    private func presentLookCard(_ descriptor: LookDescriptor,
                                 at point: CGPoint,
                                 primaryHeight: CGFloat) {
        dismissLookCard()
        let lines = Array(descriptor.lines.prefix(LookCardView.maxLines))
        guard !lines.isEmpty else { return }
        let size = CGSize(width: LookCardView.cardWidth,
                          height: LookCardView.height(for: lines))
        guard let frame = lookCardFrame(at: point, size: size,
                                        primaryHeight: primaryHeight) else {
            return
        }
        let appKitFrame = appKitRect(fromQuartz: frame,
                                     primaryHeight: primaryHeight)
        // Non-activating and click-through: the app under the pointer keeps its
        // keyboard focus, and a click only dismisses the card.
        let window = LookCardWindow(contentRect: appKitFrame,
                                    styleMask: [.borderless, .nonactivatingPanel],
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
        window.ignoresMouseEvents = true
        window.isMovable = false
        window.isMovableByWindowBackground = false
        window.isReleasedWhenClosed = false
        window.acceptsMouseMovedEvents = false
        window.hidesOnDeactivate = false

        let view = LookCardView(frame: NSRect(origin: .zero,
                                              size: appKitFrame.size),
                                lines: lines)
        view.autoresizingMask = [.width, .height]
        window.contentView = view
        lookCardWindow = window
        window.orderFrontRegardless()

        lookCardGeneration += 1
        let generation = lookCardGeneration
        let timer = Timer(timeInterval: LookCardWindow.autoDismissInterval,
                          repeats: false) { [weak self] _ in
            guard let self = self, self.lookCardGeneration == generation else {
                return
            }
            self.dismissLookCard()
        }
        RunLoop.main.add(timer, forMode: .common)
        lookCardTimer = timer
        installLookCardMonitors()
        chipJournal("look-card")
    }

    /// Any click, Esc, the 6 s timer, or the hat being used again all land here
    /// so no event monitor or timer can outlive the card.
    private func dismissLookCard() {
        lookCardGeneration += 1
        lookCardTimer?.invalidate()
        lookCardTimer = nil
        for monitor in lookCardMonitors {
            NSEvent.removeMonitor(monitor)
        }
        lookCardMonitors.removeAll()
        if let window = lookCardWindow {
            window.orderOut(nil)
            window.close()
        }
        lookCardWindow = nil
    }

    /// DEAD SINCE THE DRAG PARITY: only the unreferenced `presentLookCard` calls
    /// this. `dismissLookCard` above stays live (the hat press and the arm
    /// dismiss any card that might still be on screen).
    private func installLookCardMonitors() {
        let mouse: NSEvent.EventTypeMask = [.leftMouseDown,
                                            .rightMouseDown,
                                            .otherMouseDown]
        if let localMouse = NSEvent.addLocalMonitorForEvents(matching: mouse,
                                                             handler: { [weak self] event in
            self?.dismissLookCard()
            return event
        }) {
            lookCardMonitors.append(localMouse)
        }
        if let globalMouse = NSEvent.addGlobalMonitorForEvents(matching: mouse,
                                                               handler: { [weak self] _ in
            self?.dismissLookCard()
        }) {
            lookCardMonitors.append(globalMouse)
        }
        // Esc is the only key that dismisses; every other key is left alone so
        // the user keeps typing in their own app.
        let escape: UInt16 = 53
        let keyHandler: (NSEvent) -> Void = { [weak self] event in
            if event.keyCode == escape {
                self?.dismissLookCard()
            }
        }
        if let localKey = NSEvent.addLocalMonitorForEvents(matching: .keyDown,
                                                           handler: { event in
            keyHandler(event)
            return event
        }) {
            lookCardMonitors.append(localKey)
        }
        if let globalKey = NSEvent.addGlobalMonitorForEvents(matching: .keyDown,
                                                             handler: keyHandler) {
            lookCardMonitors.append(globalKey)
        }
    }

    // MARK: In-place ask field

    private func toggleSummonField() {
        if summonFieldOpen {
            let event = summonFieldLookTarget != nil ? "look-cancel" : "ask-cancel"
            closeSummonField(journal: event)
        } else {
            openSummonField()
        }
    }

    /// A 280x34 box: centred on the chip and 8 pt above it when the chip is
    /// visible, otherwise centred under the pet's sprite box. LOOK passes its
    /// taller height. Clamped fully on screen exactly like `claimChipFrame`.
    private func summonFieldFrame(primaryHeight: CGFloat,
                                  fieldHeight: CGFloat = SummonFieldView.fieldHeight) -> CGRect? {
        let size = CGSize(width: SummonFieldView.fieldWidth,
                          height: fieldHeight)
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

    /// The plain doors (hat click, ⌘⇧A, the LOOK submit): nothing prefilled.
    private func openSummonField() {
        openSummonField(prefill: nil)
    }

    private func openSummonField(prefill: String?) {
        // Consume LOOK state before ANY guard: an aborted open must never leave
        // a queued element behind for the next hat tap to inherit.
        let lookContext = pendingLookContext
        let lookTarget = pendingLookTarget
        pendingLookContext = nil
        pendingLookTarget = nil
        guard !isShuttingDown, !summonFieldOpen else {
            return
        }
        // LOOK mode is queued by openLookField(); the plain doors leave these
        // nil and get today's behaviour unchanged.
        let fieldHeight = SummonFieldView.height(forLook: lookContext != nil)
        guard let primaryHeight = primaryScreenHeight(),
              let frame = summonFieldFrame(primaryHeight: primaryHeight,
                                           fieldHeight: fieldHeight) else {
            chipJournal("ask-open-failed")
            return
        }
        let appKitFrame = appKitRect(fromQuartz: frame,
                                     primaryHeight: primaryHeight)
        // An open ask field is one of INSPECT MODE's exits: the mode has
        // delivered its target, so the overlays close and the hat goes home
        // while the user types. This runs AFTER the frame above was taken.
        disarmInspectMode()
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
        // Above path INSPECT MODE's overlays (.screenSaver): a leftover overlay
        // would otherwise cover the field, and the user's keystrokes (Return
        // included) would go to the app underneath — "pressing enter also
        // triggers enter in the active window".
        window.level = NSWindow.Level(rawValue: NSWindow.Level.screenSaver.rawValue + 1)
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
        view.configureForLook(context: lookContext)
        // The hat's right-click menu hands over a prefilled request (the watch
        // doors); the text stays editable and the caret lands at the end.
        if let prefill = prefill, !prefill.isEmpty {
            view.field.stringValue = prefill
        }
        view.onSubmit = { [weak self] text, destination in
            guard let self = self else { return }
            if let target = self.summonFieldLookTarget {
                self.submitLookText(text, destination: destination, target: target)
            } else {
                self.submitSummonText(text, destination: destination)
            }
        }
        view.onCancel = { [weak self] in
            self?.cancelSummonField()
        }
        // The key-equivalent path for ⌘Return. The view's guard makes sure a
        // press reported twice still emits one request.
        window.onCommandReturn = { [weak view] in
            _ = view?.emitSubmit(destination: .new)
        }
        window.contentView = view

        summonFieldWindow = window
        summonFieldView = view
        summonFieldOpen = true
        summonFieldLookTarget = lookContext != nil ? (lookTarget ?? LookTarget()) : nil
        window.orderFrontRegardless()
        // Our own panel takes key focus. This is deliberately NOT the Hermes
        // window: the companion becomes the active app just long enough to
        // type, then the previous app is restored.
        NSApp.activate(ignoringOtherApps: true)
        window.makeKey()
        window.makeFirstResponder(view.field)
        // Activation can be refused (macOS 14 refuses a background accessory
        // stealing focus mid-click). The field still DRAWS, so the user types
        // into the app underneath — including Return. Check, retry once, and
        // journal the truth so this is never guessed at again.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self, weak window] in
            guard let self = self, let window = window, self.summonFieldOpen else { return }
            if !NSApp.isActive || !window.isKeyWindow {
                NSRunningApplication.current.activate(options: [.activateIgnoringOtherApps,
                                                               .activateAllWindows])
                window.makeKey()
                window.makeFirstResponder(view.field)
            }
            chipJournal("ask-focus active=\(NSApp.isActive ? 1 : 0) key=\(window.isKeyWindow ? 1 : 0)")
        }
        if let prefill = prefill, !prefill.isEmpty,
           let editor = view.field.currentEditor() {
            // Caret at the END: the prefilled command is editable, not selected.
            editor.selectedRange = NSRange(location: (prefill as NSString).length,
                                           length: 0)
        }
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
        summonFieldLookTarget = nil
        pendingLookContext = nil
        pendingLookTarget = nil
        if let event = event {
            chipJournal(event)
        }
        // Only an explicit close (submit/Esc/toggle) hands focus back. When the
        // field resigned because the user moved to another app, that app must
        // keep focus — restoring here would steal it, the very coercion this
        // door removes.
        if restoreFocus && !isShuttingDown {
            // Re-activating the previous app on the SAME run-loop turn as the
            // Return keyDown makes macOS hand that still-held key to the app we
            // just activated: the user's Enter fires in their app too, even
            // though our field was active and key (journal: ask-focus active=1
            // key=1). Let the keystroke finish first.
            let pending = returnApp
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.30) { [weak self] in
                guard let self = self, !self.isShuttingDown else { return }
                self.restoreFrontmostApp(pending)
                chipJournal("ask-restore-delayed")
            }
        }
    }

    /// Typed LOOK path: hit-test the cursor once, then raise the SAME ask box
    /// in LOOK mode. The pointer gesture uses the card instead; both resolve
    /// through `lookElementDescriptor(at:)`. The element is captured now,
    /// before the field steals focus.
    private func openLookField() {
        guard !isShuttingDown, !summonFieldOpen else {
            return
        }
        let hit = axLookTargetUnderCursor()
        pendingLookContext = hit.context
        pendingLookTarget = hit.target
        openSummonField()
    }

    private func submitSummonText(_ raw: String,
                                  destination: SummonDestination) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // Empty text is a cancel, not an empty request on the wire.
        guard !text.isEmpty else {
            closeSummonField(journal: "ask-cancel")
            return
        }
        let payload: [String: Any] = [
            "op": "summon_text",
            "text": text,
            "id": String(format: "%08x", arc4random()),
            "destination": destination.rawValue
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
        // File spool first: the companion's stdout goes to whichever plugin host
        // spawned it, and that pipe handle has gone stale in practice (write
        // succeeds, nobody reads). The spool cannot lose its reader — the
        // dashboard API merges it into /state and the desktop acks it.
        appendSummonSpool(line)
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
        // Name the app we are about to reactivate: if the user reports Return
        // firing there too, the journal says where, without a guess.
        let previousAppName = summonFieldReturnApp?.localizedName ?? "none"
        chipJournal("ask-submit:\(text.count)chars:\(destination.rawValue):back=\(previousAppName)")
        closeSummonField(journal: nil)
    }

    /// LOOK submit: the question plus the element context the hit-test found.
    /// Same one-line protocol and the same file spool as the ask field.
    private func submitLookText(_ raw: String,
                                destination: SummonDestination,
                                target: LookTarget) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            closeSummonField(journal: "look-cancel")
            return
        }
        let payload: [String: Any] = [
            "op": "look_text",
            "text": text,
            "id": String(format: "%08x", arc4random()),
            "destination": destination.rawValue,
            "target": target.payload
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: []),
              let line = String(data: data, encoding: .utf8) else {
            chipJournal("look-submit-failed")
            closeSummonField(journal: nil)
            return
        }
        appendSummonSpool(line)
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
        chipJournal("look-submit:\(text.count)chars")
        closeSummonField(journal: nil)
    }

    /// Append one request to the file spool the dashboard API reads.
    /// Bounded to the last 50 rows so a never-acked request cannot grow the file.
    private func appendSummonSpool(_ line: String) {
        let dir = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".hermes/plugin-data/butler")
        let path = (dir as NSString).appendingPathComponent("summon-spool.jsonl")
        var rows = (try? String(contentsOfFile: path, encoding: .utf8))?
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init) ?? []
        rows.append(line)
        if rows.count > 50 {
            rows.removeFirst(rows.count - 50)
        }
        try? (rows.joined(separator: "\n") + "\n").write(toFile: path,
                                                        atomically: true,
                                                        encoding: .utf8)
    }

    private func cancelSummonField() {
        guard summonFieldOpen else {
            return
        }
        let event = summonFieldLookTarget != nil ? "look-cancel" : "ask-cancel"
        closeSummonField(journal: event)
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
        let event = summonFieldLookTarget != nil ? "look-cancel" : "ask-cancel"
        closeSummonField(journal: event, restoreFocus: false)
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

    // MARK: INSPECT MODE

    /// Right-click the hat (or drag it past the threshold, with `releaseToAsk`):
    /// every display gets a screen-sized click catcher, the hat is hidden, and
    /// the badge under the pointer names the control a click/release would ask
    /// about. Pointer-only: no focus, no first responder. The only key this mode
    /// ever reads is Esc, through the two monitors installed below.
    private func armInspectMode(releaseToAsk: Bool = false) {
        // Never behind an open ask field: the field and the overlays would both
        // want the pointer, and every door into the field disarms the mode.
        guard !inspectMode, !summonFieldOpen, !isShuttingDown,
              claimChipEnabled else { return }
        guard claimChipWindow != nil, claimChipView != nil else { return }
        // Another of our windows on screen would sit in the read path.
        dismissLookCard()
        inspectMode = true
        inspectReleaseToAsk = releaseToAsk
        inspectResolving = false
        inspectArmPoint = cursorQuartzPoint()
        inspectLastTestedPoint = nil
        inspectBadgeDetail = ""
        NSCursor.crosshair.push()
        // The hat is an indicator only while the mode is up; the overlay owns
        // the pointer. Hidden, not torn down: disarm puts it back on the pet.
        claimChipWindow?.orderOut(nil)
        // A drag is already in flight from the press that armed the mode, so the
        // release catcher must exist before the overlays do.
        installInspectMonitors(releaseToAsk: releaseToAsk)
        if releaseToAsk {
            chipJournal("drag-arm")
        } else {
            chipJournal("inspect-arm")
        }
        raiseInspectOverlays()
        // No screen means no click catcher and no way to leave the mode except
        // the timeout: do not arm at all.
        guard !inspectWindows.isEmpty else {
            disarmInspectMode()
            return
        }
        installInspectTimers()
        // Place the badge on the current pointer at once: waiting up to 100 ms
        // leaves the hat visible at the pet after the click.
        inspectTick()
    }

    /// One click catcher per screen, covering that screen's full frame. Nothing
    /// becomes key or main, so the user's app keeps keyboard focus.
    private func raiseInspectOverlays() {
        for screen in NSScreen.screens {
            let frame = screen.frame
            guard frame.width > 0.0, frame.height > 0.0 else { continue }
            let window = InspectOverlayWindow(contentRect: frame,
                                              styleMask: [.borderless, .nonactivatingPanel],
                                              backing: .buffered,
                                              defer: false)
            window.isOpaque = false
            window.backgroundColor = NSColor.black.withAlphaComponent(0.05)
            window.hasShadow = false
            // Above every app and the menu bar: a click anywhere means US.
            window.level = .screenSaver
            window.collectionBehavior = [.canJoinAllSpaces,
                                         .stationary,
                                         .fullScreenAuxiliary,
                                         .ignoresCycle]
            window.ignoresMouseEvents = false
            window.isMovable = false
            window.isMovableByWindowBackground = false
            window.isReleasedWhenClosed = false
            window.acceptsMouseMovedEvents = false
            window.hidesOnDeactivate = false

            let view = InspectOverlayView(frame: NSRect(origin: .zero, size: frame.size))
            view.autoresizingMask = [.width, .height]
            view.onInspectClick = { [weak self] point in
                self?.inspectClicked(at: point)
            }
            view.onCancel = { [weak self] in
                self?.cancelInspectMode(reason: "right-click")
            }
            view.onBadgeClick = { [weak self] in
                self?.cancelInspectMode(reason: "badge")
            }
            window.contentView = view
            inspectWindows.append(window)
            inspectViews.append(view)
            window.orderFrontRegardless()
        }
    }

    private func installInspectTimers() {
        let tick = Timer(timeInterval: InspectModeSpec.tickInterval,
                         repeats: true) { [weak self] _ in
            self?.inspectTick()
        }
        RunLoop.main.add(tick, forMode: .common)
        inspectTickTimer = tick
        let timeout = Timer(timeInterval: InspectModeSpec.timeout,
                            repeats: false) { [weak self] _ in
            self?.inspectTimedOut()
        }
        RunLoop.main.add(timeout, forMode: .common)
        inspectTimeoutTimer = timeout
    }

    /// Esc cancels in every LOOK path, so the mode installs a local AND a global
    /// key monitor at arm. Only keyCode 53 is read; every other key is passed
    /// through untouched, so the app underneath keeps its keyboard. The look
    /// card proves a global monitor works here: the companion already holds
    /// Accessibility trust, so this needs no new or wider privacy grant.
    ///
    /// A drag-armed mode additionally watches this app's own mouseUp. The chip
    /// window is hidden while the mode is up, and AppKit drops a mouseUp whose
    /// window was ordered out mid-gesture — which would pin the mode until the
    /// timeout and lose the release the user aimed.
    private func installInspectMonitors(releaseToAsk: Bool) {
        let escape: UInt16 = 53
        if let localKey = NSEvent.addLocalMonitorForEvents(matching: .keyDown,
                                                           handler: { [weak self] event in
            guard event.keyCode == escape else { return event }
            // Swallow it ONLY when this mode consumed it: cancelling must not
            // also cancel something in the app the user was working in, and a
            // monitor that outlived the mode must never eat Esc blindly.
            return (self?.escapeInspectMode() ?? false) ? nil : event
        }) {
            inspectMonitors.append(localKey)
        }
        if let globalKey = NSEvent.addGlobalMonitorForEvents(matching: .keyDown,
                                                             handler: { [weak self] event in
            if event.keyCode == escape {
                self?.escapeInspectMode()
            }
        }) {
            inspectMonitors.append(globalKey)
        }
        guard releaseToAsk else { return }
        if let localUp = NSEvent.addLocalMonitorForEvents(matching: .leftMouseUp,
                                                          handler: { [weak self] event in
            // Observed, not swallowed: the chip's own mouseUp still has to
            // clear its press latch after the mode has resolved.
            self?.inspectReleasedAtMouseUp()
            return event
        }) {
            inspectMonitors.append(localUp)
        }
    }

    /// One Esc path for both arm modes: journal the hop, then leave the mode
    /// (which puts the hat back on the pet). Returns whether it actually
    /// cancelled, so the monitor only swallows an Esc this mode consumed.
    @discardableResult
    private func escapeInspectMode() -> Bool {
        guard inspectMode else { return false }
        chipJournal("inspect-escape")
        cancelInspectMode(reason: "escape")
        return true
    }

    /// Right-click, a badge click, the ask opening or teardown all land here, so
    /// no timer or overlay outlives the mode. A cancel is a mode exit, hence the
    /// hat returning to the pet.
    private func cancelInspectMode(reason: String) {
        guard inspectMode else { return }
        chipJournal("inspect-cancel:" + reason)
        disarmInspectMode()
    }

    private func inspectTimedOut() {
        guard inspectMode else { return }
        chipJournal("inspect-timeout")
        cancelInspectMode(reason: "timeout")
    }

    private func disarmInspectMode() {
        guard inspectMode else { return }
        inspectMode = false
        inspectResolving = false
        inspectReleaseToAsk = false
        inspectTickTimer?.invalidate()
        inspectTickTimer = nil
        inspectTimeoutTimer?.invalidate()
        inspectTimeoutTimer = nil
        // The Esc monitors (and, for a drag arm, the release catcher) live only
        // as long as the mode: remove every one before the overlays go.
        for monitor in inspectMonitors {
            NSEvent.removeMonitor(monitor)
        }
        inspectMonitors.removeAll()
        for window in inspectWindows {
            window.orderOut(nil)
            window.close()
        }
        inspectWindows.removeAll()
        inspectViews.removeAll()
        // Leave no crosshair behind: pop the cursor pushed at arm, then drop the
        // stale cursor rects so the app underneath gets its own cursor back.
        NSCursor.pop()
        NSCursor.arrow.set()
        inspectArmPoint = nil
        inspectLastTestedPoint = nil
        inspectBadgeDetail = ""
        chipJournal("inspect-disarm")
        // The chip was hidden for the whole mode, so a mouseUp that never
        // reached it must not leave the press latch or the 👀 draw behind.
        claimChipDragging = false
        claimChipView?.cancelPendingPress()
        // The hat returns to the pet.
        positionClaimChip()
        claimChipWindow?.orderFrontRegardless()
    }

    /// The 10 Hz loop: move the badge, then — only when the pointer moved ≥ 6 pt
    /// since the last test — take ONE AX read of the point under it.
    private func inspectTick() {
        guard inspectMode, !inspectResolving, !isShuttingDown else { return }
        guard let primaryHeight = primaryScreenHeight() else { return }
        let mouse = NSEvent.mouseLocation
        guard mouse.x.isFinite, mouse.y.isFinite else { return }
        let quartz = CGPoint(x: mouse.x, y: primaryHeight - mouse.y)

        if let last = inspectLastTestedPoint {
            let dx = quartz.x - last.x
            let dy = quartz.y - last.y
            if (dx * dx + dy * dy).squareRoot() < InspectModeSpec.moveThreshold {
                updateInspectBadges(cursor: mouse)
                return
            }
        }
        // Never AX-test a point under our own badge: our windows win every
        // hit-test and a read-out pill is not a target (§8). The badge placement
        // keeps the cursor outside its rect, so this guard is belt and braces.
        if inspectBadgeContains(quartz, primaryHeight: primaryHeight) {
            updateInspectBadges(cursor: mouse)
            return
        }
        inspectLastTestedPoint = quartz
        // The overlay is topmost everywhere, so a system-wide hit-test would
        // answer with it. Ask the app that owns the window UNDER the overlay
        // instead: an application-scoped hit-test is restricted to that app
        // (AXUIElement.h), so the preview needs no parking and no dim flicker.
        if let descriptor = inspectDescriptorUnderCursor(at: quartz),
           !lookDescriptorIsSelf(descriptor) {
            inspectBadgeDetail = inspectTargetLine(descriptor)
        } else {
            // Nothing readable under the pointer (desktop, AX refusal, our own
            // second companion): never keep naming the last control seen.
            inspectBadgeDetail = ""
        }
        updateInspectBadges(cursor: mouse)
    }

    /// Feed each overlay view the pointer in ITS coordinates. Only the display
    /// under the pointer carries the badge: a pill clamped onto another screen
    /// would be a ghost. The gesture line is honest about how this arm resolves.
    private func updateInspectBadges(cursor: NSPoint) {
        let title = inspectReleaseToAsk ? InspectModeSpec.releaseTitle
                                        : InspectModeSpec.clickTitle
        for view in inspectViews {
            guard let window = view.window else { continue }
            let local = view.convert(window.convertPoint(fromScreen: cursor), from: nil)
            if view.bounds.contains(local) {
                view.updateBadge(cursor: local,
                                 detail: inspectBadgeDetail,
                                 title: title)
            } else {
                view.updateBadge(cursor: nil,
                                 detail: inspectBadgeDetail,
                                 title: title)
            }
        }
    }

    /// True when a Quartz point lands inside the badge drawn on our overlay.
    private func inspectBadgeContains(_ quartz: CGPoint,
                                      primaryHeight: CGFloat) -> Bool {
        let appKitPoint = NSPoint(x: quartz.x, y: primaryHeight - quartz.y)
        for view in inspectViews {
            guard let window = view.window, window.isVisible else { continue }
            let local = view.convert(window.convertPoint(fromScreen: appKitPoint),
                                     from: nil)
            guard view.bounds.contains(local) else { continue }
            return view.badgeFrame.contains(local)
        }
        return false
    }

    /// The pid of the topmost window at `point` that is not ours: what the user
    /// is actually looking at while our overlay covers the display.
    private func inspectUnderlyingPid(at point: CGPoint) -> pid_t? {
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let info = CGWindowListCopyWindowInfo(options,
                                                    kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        let me = ProcessInfo.processInfo.processIdentifier
        for window in info {
            guard let rawPid = intValue(window[kCGWindowOwnerPID as String]),
                  rawPid > 0, pid_t(rawPid) != me,
                  let alpha = numberValue(window[kCGWindowAlpha as String]),
                  alpha > 0.0,
                  let bounds = windowBounds(window),
                  bounds.contains(point) else {
                continue
            }
            return pid_t(rawPid)
        }
        return nil
    }

    /// Read the control under `point` from the app BELOW our overlay. The
    /// system-wide element would answer with this process's overlay window, so
    /// the hit-test is scoped to the underlying app instead.
    private func inspectDescriptorUnderCursor(at point: CGPoint) -> LookDescriptor? {
        guard let pid = inspectUnderlyingPid(at: point) else { return nil }
        let app = AXUIElementCreateApplication(pid)
        var element: AXUIElement?
        let error = AXUIElementCopyElementAtPosition(app,
                                                     Float(point.x),
                                                     Float(point.y),
                                                     &element)
        guard error == .success, let hit = element else { return nil }
        return lookDescriptor(for: hit, at: point)
    }

    /// Left click on the overlay. The click is captured on purpose: at this
    /// point the topmost window is OUR overlay, so the control under it never
    /// fires and the question becomes "what did I just click?". A drag-armed
    /// mode confirms on RELEASE instead: a press here is the tail of the drag
    /// that armed it, so it must not resolve early.
    private func inspectClicked(at screenPoint: NSPoint) {
        guard inspectMode, !inspectResolving, !isShuttingDown,
              !inspectReleaseToAsk else { return }
        guard let primaryHeight = primaryScreenHeight(),
              screenPoint.x.isFinite, screenPoint.y.isFinite else {
            cancelInspectMode(reason: "no-point")
            return
        }
        // A click that never left the arming point tests THAT point (the
        // pointer never moved); any real travel uses the click itself. Either
        // way the descriptor is resolved FRESH after parking — never reuse a
        // pre-arm or pre-click read-out.
        let clicked = CGPoint(x: screenPoint.x, y: primaryHeight - screenPoint.y)
        var point = clicked
        if let arm = inspectArmPoint {
            let dx = clicked.x - arm.x
            let dy = clicked.y - arm.y
            if (dx * dx + dy * dy).squareRoot() < InspectModeSpec.moveThreshold {
                point = arm
            }
        }
        inspectResolving = true
        inspectTickTimer?.invalidate()
        inspectTickTimer = nil
        inspectTimeoutTimer?.invalidate()
        inspectTimeoutTimer = nil
        // §8: our own windows win every AX hit-test, so every overlay is parked
        // off-screen (orderOut alone is applied by the window server on the NEXT
        // pass) and the read happens on the next run-loop turn.
        for window in inspectWindows {
            window.setFrameOrigin(NSPoint(x: -20000.0, y: -20000.0))
        }
        DispatchQueue.main.async { [weak self] in
            self?.resolveInspectClick(at: point, retries: 1)
        }
    }

    /// Read the click point with the overlays parked, tolerating the window
    /// server still reporting our own window for a beat. Never answers with
    /// "ButlerOverlay".
    private func resolveInspectClick(at point: CGPoint, retries: Int) {
        guard inspectMode else { return }
        var descriptor = lookElementDescriptor(at: point)
        if lookDescriptorIsSelf(descriptor), retries > 0 {
            // One short retry while the parked overlays settle; they stay
            // off-screen until this resolves.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
                self?.resolveInspectClick(at: point, retries: retries - 1)
            }
            return
        }
        if lookDescriptorIsSelf(descriptor) {
            descriptor = lookDescriptorForActiveApp()
            chipJournal("inspect-self-hit-fallback")
        }
        // The last resort must not be us either: an accessory helper can be the
        // menu-bar owner's fallback when nothing else is focused. Asking the
        // butler about itself is never the answer, so exit instead.
        if lookDescriptorIsSelf(descriptor) {
            cancelInspectMode(reason: "self-after-fallback")
            return
        }
        // The journal names the confirmation the user actually made: a release
        // for a drag arm, a click for a right-click arm.
        if inspectReleaseToAsk {
            chipJournal("drag-release:" + (descriptor.role ?? "?"))
        } else {
            chipJournal("inspect-click:" + (descriptor.role ?? "?"))
        }
        let target = LookTarget(app: descriptor.app,
                                role: descriptor.role,
                                label: descriptor.label,
                                window: descriptor.window,
                                doc: descriptor.doc,
                                enabled: descriptor.enabled,
                                box: descriptor.box)
        let line = target.contextLine
        pendingLookContext = line.isEmpty ? nil : line
        pendingLookTarget = line.isEmpty ? nil : target
        // Close the overlays and put the hat back BEFORE the field opens: the
        // field is placed against the pet, and the mode must not outlive the
        // click. The existing LOOK door then raises the same ask box the drag
        // route uses.
        disarmInspectMode()
        openSummonField()
    }

    /// The hat's right-click: INSPECT MODE, or the LOOK/watch menu with Option.
    /// (The hat is hidden while the mode is up, so the in-mode branch is a belt-
    /// and-braces exit for a stale visible chip.)
    private func claimChipRightClicked(at point: NSPoint) {
        claimChipDragging = false
        claimChipView?.cancelPendingPress()
        if inspectMode {
            cancelInspectMode(reason: "right-click")
            return
        }
        // A menu, because a hidden chord is not a UI: "Option-right-click" was
        // undiscoverable in practice. Right-click lists every action with
        // `Look at this…` first; dragging the hat is the direct LOOK gesture, so
        // the one-move path stays a drag rather than a lost shortcut.
        presentHatMenu(at: point)
    }

    /// `Look at this…` arms INSPECT MODE; the three watch items open the SAME
    /// ask field prefilled, so they ride the normal agent turn with no new
    /// plumbing.
    private func presentHatMenu(at point: NSPoint) {
        guard !isShuttingDown, !summonFieldOpen, let view = claimChipView else {
            return
        }
        let frontmost = hatMenuFrontmostAppName()
        let app = frontmost ?? "this app"
        // The `watch` row in state.json is the ground truth; the in-process
        // observer covers an unreadable file.
        let watchActive = watchIsActive()
        // The announcement outlives the watch row, so its enablement comes from
        // the top-level `watch_hit` block, not from `watch`.
        let hitPending = unacknowledgedWatchHit()
        let menu = NSMenu()
        menu.autoenablesItems = false

        var targets: [HatMenuItemTarget] = []
        func entry(_ title: String,
                   enabled: Bool = true,
                   action: @escaping () -> Void) -> NSMenuItem {
            let target = HatMenuItemTarget(action)
            targets.append(target)
            let item = NSMenuItem(title: title,
                                  action: #selector(HatMenuItemTarget.fire(_:)),
                                  keyEquivalent: "")
            item.target = target
            item.isEnabled = enabled
            return item
        }

        menu.addItem(entry("Look at this…") { [weak self] in
            self?.armInspectMode()
        })
        menu.addItem(.separator())
        // No nameable frontmost app: keep the row visible but disabled rather
        // than prefilling a watch for "this app", which can never resolve.
        menu.addItem(entry("Watch \(app)…", enabled: frontmost != nil) { [weak self] in
            self?.openSummonField(prefill: "watch \(app) for ")
        })
        menu.addItem(entry("What did the watch see?", enabled: watchActive) { [weak self] in
            self?.openSummonField(prefill: "what did the watch see?")
        })
        // The hat IS the acknowledgement button for a hit that outlived its
        // watch; disabled only when there is nothing unacknowledged to clear.
        menu.addItem(entry("Acknowledge watch hit", enabled: hitPending) { [weak self] in
            self?.acknowledgeWatchHit()
        })
        menu.addItem(entry("Stop watching", enabled: watchActive) { [weak self] in
            self?.openSummonField(prefill: "stop watching")
        })

        // The boxes must outlive the tracking loop (an item's target is weak).
        // Replaced on the next menu rather than cleared: the action is delivered
        // as the menu closes, and holding a handful of boxes one menu longer is
        // free.
        hatMenuTargets = targets
        menu.popUp(positioning: nil, at: point, in: view)
    }

    /// The app a watch would attach to: the menu-bar owner, never this helper
    /// (an accessory process never owns the menu bar).
    private func hatMenuFrontmostAppName() -> String? {
        let me = ProcessInfo.processInfo.processIdentifier
        if let app = NSWorkspace.shared.menuBarOwningApplication,
           app.processIdentifier != me {
            return axNonEmpty(app.localizedName)
        }
        if let app = NSWorkspace.shared.frontmostApplication,
           app.processIdentifier != me {
            return axNonEmpty(app.localizedName)
        }
        return nil
    }

    /// Is a watch active? The `watch` row in state.json is the ground truth and
    /// costs one small file read on a right-click. The engine writes
    /// `"stopped": null` while the watch runs and a block once it stopped, and
    /// JSON null arrives as NSNull — not Swift nil — so both must count as
    /// active. With no readable file the companion's own observer decides.
    private func watchIsActive() -> Bool {
        if let row = readWatchStateRow() {
            let stopped = row["stopped"]
            return stopped == nil || stopped is NSNull
        }
        return watchObserver != nil
    }

    /// One line on stdout: the hat's acknowledgement button. The Python side
    /// routes it to the same engine door `butler_watch_ack` uses — no new
    /// window, no new transport and no HTTP from this helper. The menu closes
    /// itself when the item fires.
    private func acknowledgeWatchHit() {
        chipJournal("watch-ack:emit")
        writeEventLine(["op": "watch_ack"])
    }

    /// The `watch` row of the shared state snapshot, or nil when unreadable.
    private func readWatchStateRow() -> [String: Any]? {
        guard let object = readButlerState() else {
            return nil
        }
        return object["watch"] as? [String: Any]
    }

    /// Is there a `watch_hit` announcement the user has not acknowledged? The
    /// engine writes the block on a hit and keeps it after the watch stops, so
    /// one cheap file read on right-click is the whole gate.
    private func unacknowledgedWatchHit() -> Bool {
        guard let object = readButlerState(),
              let hit = object["watch_hit"] as? [String: Any] else {
            return false
        }
        let acknowledged = hit["acknowledged"]
        if acknowledged == nil || acknowledged is NSNull {
            return true
        }
        if let flag = acknowledged as? Bool {
            return !flag
        }
        return true
    }

    /// The shared state snapshot from the plugin data dir, or nil.
    private func readButlerState() -> [String: Any]? {
        let dir = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".hermes/plugin-data/butler")
        let path = (dir as NSString).appendingPathComponent("state.json")
        guard let data = FileManager.default.contents(atPath: path),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return nil
        }
        return object
    }

    // MARK: System-wide summon hot key

    /// One Carbon handler for the single remaining global chord, ⌘⇧A. LOOK is
    /// a pointer gesture and registers nothing.
    private func installHotKeyHandlerIfNeeded() -> Bool {
        if summonHotKeyHandler != nil {
            return true
        }
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                      eventKind: UInt32(kEventHotKeyPressed))
        let handler: EventHandlerUPP = { _, event, userData in
            guard let event = event, let userData = userData else {
                return noErr
            }
            var hotKeyID = EventHotKeyID()
            let status = GetEventParameter(event,
                                           EventParamName(kEventParamDirectObject),
                                           EventParamType(typeEventHotKeyID),
                                           nil,
                                           MemoryLayout<EventHotKeyID>.size,
                                           nil,
                                           &hotKeyID)
            guard status == noErr else {
                return noErr
            }
            let controller = Unmanaged<OverlayController>
                .fromOpaque(userData)
                .takeUnretainedValue()
            if hotKeyID.id == 1 {
                controller.summonHotKeyFired()
            }
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
            return false
        }
        return true
    }

    /// Register ⌘⇧A with Carbon so it works from any app. The desktop keybind
    /// stays as the in-app path; when Hermes is already frontmost this handler
    /// steps aside so the two never double-open the bar.
    private func installSummonHotKey() {
        guard summonHotKeyRef == nil else {
            return
        }
        guard installHotKeyHandlerIfNeeded() else {
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
        // A registered Carbon hot key SWALLOWS the chord system-wide, so the
        // in-app keybind never sees it while the companion is running. Opening
        // the field unconditionally is therefore the only correct behaviour:
        // stepping aside here made ⌘⇧A dead inside Hermes. The in-app bar
        // remains reachable from the titlebar launcher, and from the keybind
        // itself whenever the companion is not running.
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
              petWindow == nil,
              // An ambient watch is work: it may legitimately idle for hours
              // between two changes, so the idle watchdog must not retire the
              // observer (and silently end the watch) after 10 minutes.
              watchObserver == nil else {
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
        guard !isShuttingDown, activeRing == nil, chipWindow == nil,
              watchObserver == nil else {
            return
        }
        cancelPendingExit()
        let generation = pendingExitGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + postFarewellExitGrace) { [weak self] in
            guard let self = self,
                  !self.isShuttingDown,
                  self.pendingExitGeneration == generation,
                  self.activeRing == nil,
                  self.chipWindow == nil,
                  self.watchObserver == nil else {
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
        // Kill INSPECT MODE's timers and screen-sized overlays before anything
        // else tears down.
        disarmInspectMode()
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
        // A watch holds an AXObserver + run-loop source; never leak them past
        // shutdown (also covers SIGTERM and stdin EOF).
        stopWatch(reason: "shutdown", emit: false)
        removeSummonHotKey()
        dismissLookCard()
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
