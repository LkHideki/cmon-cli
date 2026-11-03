// Draws the cmon Bar app icon into an .iconset (install.sh turns it into AppIcon.icns).
// A quota gauge: outer ring = weekly window, inner ring = 5h window, on a warm dark tile.
// Usage: swift make_icon.swift <out.iconset>

import AppKit

let tileTop = NSColor(srgbRed: 0.20, green: 0.19, blue: 0.18, alpha: 1)
let tileBottom = NSColor(srgbRed: 0.10, green: 0.10, blue: 0.09, alpha: 1)
let track = NSColor(white: 1, alpha: 0.10)
let weekly = NSColor(srgbRed: 0.85, green: 0.47, blue: 0.34, alpha: 1)  // terracotta
let session = NSColor(srgbRed: 0.96, green: 0.93, blue: 0.87, alpha: 1)  // warm ivory
let weeklyShare = 0.72, sessionShare = 0.44  // how full each ring is drawn

func arc(_ center: NSPoint, _ radius: CGFloat, _ share: Double, _ width: CGFloat, _ color: NSColor) {
    let path = NSBezierPath()
    path.appendArc(withCenter: center, radius: radius, startAngle: 90,
                   endAngle: 90 - 360 * share, clockwise: true)
    path.lineWidth = width
    path.lineCapStyle = .round
    color.setStroke()
    path.stroke()
}

func draw(_ px: Int) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(px)

    // macOS icon grid: 824/1024 tile, ~22.5% corner radius, soft drop shadow.
    let tile = NSRect(x: s * 0.098, y: s * 0.098, width: s * 0.805, height: s * 0.805)
    let shape = NSBezierPath(roundedRect: tile, xRadius: s * 0.181, yRadius: s * 0.181)
    let shadow = NSShadow()
    shadow.shadowColor = NSColor(white: 0, alpha: 0.35)
    shadow.shadowBlurRadius = s * 0.02
    shadow.shadowOffset = NSSize(width: 0, height: -s * 0.008)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    tileBottom.setFill()
    shape.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: tileTop, ending: tileBottom)!.draw(in: shape, angle: -90)

    let c = NSPoint(x: s / 2, y: s / 2)
    let w = s * 0.074
    for (radius, share, color) in [(s * 0.285, weeklyShare, weekly), (s * 0.175, sessionShare, session)] {
        arc(c, radius, 1, w, track)
        arc(c, radius, share, w, color)
    }
    session.setFill()
    NSBezierPath(ovalIn: NSRect(x: c.x - w * 0.42, y: c.y - w * 0.42, width: w * 0.84, height: w * 0.84)).fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let name = scale == 1 ? "icon_\(base)x\(base).png" : "icon_\(base)x\(base)@2x.png"
        try draw(base * scale).representation(using: .png, properties: [:])!.write(to: out.appendingPathComponent(name))
    }
}
