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

import AppKit
import CoreGraphics

// MARK: - Small helpers

private func logStderr(_ message: String) {
    if let data = (message + "\n").data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
}

private func numberValue(_ value: Any?) -> Double? {
    guard let value = value else {
        return nil
    }
    if value is NSNull {
        return nil
    }
    if let number = value as? NSNumber {
        // JSON booleans bridge to NSNumber too; they are not valid geometry.
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        return number.doubleValue
    }
    if let double = value as? Double {
        return double
    }
    if let int = value as? Int {
        return Double(int)
    }
    return nil
}

private func intValue(_ value: Any?) -> Int? {
    guard let value = value else {
        return nil
    }
    if value is NSNull {
        return nil
    }
    if let number = value as? NSNumber {
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        return number.intValue
    }
    if let int = value as? Int {
        return int
    }
    if let double = value as? Double {
        return Int(double)
    }
    return nil
}

private func stringValue(_ value: Any?) -> String? {
    guard let value = value else {
        return nil
    }
    if value is NSNull {
        return nil
    }
    return value as? String
}

private func primaryScreen() -> NSScreen? {
    let screens = NSScreen.screens
    if let primary = screens.first(where: { $0.frame.origin == .zero }) {
        return primary
    }
    return screens.first
}

private func primaryScreenHeight() -> CGFloat {
    if let screen = primaryScreen() {
        return screen.frame.height
    }
    return 0.0
}

/// Convert a global Quartz (top-left origin) rect into a global AppKit
/// (bottom-left origin) rect. The primary display is the shared origin for
/// both coordinate spaces.
private func appKitRect(fromQuartz rect: CGRect) -> NSRect {
    let height = primaryScreenHeight()
    return NSRect(x: rect.origin.x,
                  y: height - rect.maxY,
                  width: rect.width,
                  height: rect.height)
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
        logStderr("butler-overlay: dropped non-UTF8 line")
        return nil
    }

    let json: Any
    do {
        json = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
    } catch {
        logStderr("butler-overlay: malformed JSON line: \(error.localizedDescription)")
        return nil
    }

    guard let object = json as? [String: Any] else {
        logStderr("butler-overlay: malformed JSON line: expected an object")
        return nil
    }
    guard let op = stringValue(object["op"]) else {
        logStderr("butler-overlay: malformed JSON line: missing string op")
        return nil
    }

    switch op {
    case "ring":
        guard let rawBox = object["box"] as? [Any], rawBox.count == 4,
              let x = numberValue(rawBox[0]),
              let y = numberValue(rawBox[1]),
              let width = numberValue(rawBox[2]),
              let height = numberValue(rawBox[3]),
              width > 0.0, height > 0.0 else {
            logStderr("butler-overlay: ring op dropped: invalid box")
            return nil
        }
        let box = CGRect(x: x, y: y, width: width, height: height)
        let spec = RingSpec(box: box,
                            label: stringValue(object["label"]),
                            say: stringValue(object["say"]),
                            step: intValue(object["step"]) ?? 0,
                            total: intValue(object["total"]) ?? 0)
        return .ring(spec)

    case "chip":
        return .chip(intValue(object["step"]) ?? 0)

    case "idle":
        return .idle

    case "quit":
        return .quit

    default:
        // Unknown operations are ignored by design.
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
            // Reserve 10pt padding on each side, so the bubble never exceeds
            // the required 420pt maximum width.
            let textMaxWidth = 400.0
            let textBounds = textSize(say,
                                      font: bubbleFont,
                                      maxWidth: textMaxWidth,
                                      paragraphStyle: bubbleStyle)
            let bubbleWidth = min(420.0, textBounds.width + 20.0)
            let bubbleHeight = textBounds.height + 16.0
            let bubbleRect = CGRect(x: ring.minX,
                                    y: ring.maxY + 10.0,
                                    width: bubbleWidth,
                                    height: bubbleHeight)
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

    init(frame: NSRect, layout: RingLayout) {
        self.layout = layout
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

        let origin = layout.canvasRect.origin
        let ringLocal = layout.ringRect.offsetBy(dx: -origin.x, dy: -origin.y)
        let chipLocal = layout.chipRect.offsetBy(dx: -origin.x, dy: -origin.y)

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
        (layout.stepText as NSString).draw(in: chipTextRect, withAttributes: chipAttributes)

        // (c) Speech bubble below the ring, max width 420pt.
        if let bubble = layout.bubbleRect {
            let bubbleLocal = bubble.offsetBy(dx: -origin.x, dy: -origin.y)
            let bubblePath = NSBezierPath(roundedRect: bubbleLocal, xRadius: 8.0, yRadius: 8.0)
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
                              width: bounds.width - 20.0,
                              height: textHeight)
        (text as NSString).draw(in: textRect, withAttributes: attributes)
    }
}

// MARK: - Overlay controller

private final class OverlayController {
    private var ringWindows: [NSWindow] = []
    private var chipWindow: NSWindow?
    private var stdinThread: Thread?

    func start() {
        guard stdinThread == nil else {
            return
        }
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
        DispatchQueue.main.async {
            NSApplication.shared.terminate(nil)
        }
    }

    private func handle(_ command: OverlayCommand) {
        switch command {
        case .ring(let spec):
            showRing(spec)
        case .chip(let step):
            showChip(step: step)
        case .idle:
            hideRings(close: false)
            hideChip()
        case .quit:
            hideRings(close: true)
            hideChip()
            NSApplication.shared.terminate(nil)
        }
    }

    // MARK: Ring

    private func showRing(_ spec: RingSpec) {
        hideRings(close: true)

        let layout = RingLayout(spec: spec)
        let canvas = layout.canvasRect
        guard canvas.width > 0.0, canvas.height > 0.0 else {
            logStderr("butler-overlay: ring op dropped: empty canvas")
            return
        }

        let frame = appKitRect(fromQuartz: canvas)
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
        window.title = spec.label ?? ""

        let content = RingView(frame: NSRect(origin: .zero, size: frame.size),
                               layout: layout)
        content.autoresizingMask = [.width, .height]
        window.contentView = content

        ringWindows.append(window)

        // orderFrontRegardless presents without activating the app, so the
        // overlay never steals focus or key status from the user's app.
        window.orderFrontRegardless()
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
}

// MARK: - Entry point

private func runOverlay() {
    let application = NSApplication.shared
    _ = application.setActivationPolicy(.accessory)

    let delegate = AppDelegate()
    application.delegate = delegate

    application.run()

    // Keep the delegate alive for the whole run loop.
    withExtendedLifetime(delegate) {
    }
}

runOverlay()
