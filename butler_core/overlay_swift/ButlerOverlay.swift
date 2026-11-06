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
//    {"op":"idle"}
//    {"op":"quit"}
//
//  Coordinates in "box" are global Accessibility (Quartz) screen coordinates:
//  origin at the top-left of the primary display, y increasing downward.
//
//  The renderer tears down its windows on EOF, "quit", SIGINT, and SIGTERM.
//

import AppKit
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

// MARK: - Parsed commands

private struct RingSpec {
    let box: CGRect
    let label: String?
    let say: String?
    let step: Int
    let total: Int
}

private enum OverlayCommand {
    case ring(RingSpec)
    case chip(Int)
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

// MARK: - Overlay controller

private final class OverlayController {
    private var ringWindows: [NSWindow] = []
    private var chipWindow: NSWindow?
    private var stdinThread: Thread?
    private var activeRing: RingSpec?
    private var observerTokens: [(NotificationCenter, NSObjectProtocol)] = []
    private var isShuttingDown = false

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
