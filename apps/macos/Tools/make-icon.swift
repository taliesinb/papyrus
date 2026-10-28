// make-icon: render the glyph on a macOS-style rounded tile. Run by build.py.
//
//   make-icon <glyph.svg> --iconset <out.iconset>     every size iconutil wants
//   make-icon <glyph.svg> --png <size> <out.png>      a full-bleed square (apple-touch-icon)
//   options: --glyph-color #hex --tile-top #hex --tile-bottom #hex
//
// Geometry follows Apple's Big Sur template: the tile fills 82.4 % of the
// canvas with a 22.4 % corner radius. The glyph's viewBox is drawn at
// --glyph-scale of the tile (default 0.84; boxicons pad their 24-unit box by
// 2 units a side, so the mark spans 70 % of the tile: as heavy as Chrome's circle).
import Cocoa

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func color(_ hex: String) -> NSColor {
    var text = hex.trimmingCharacters(in: .whitespaces)
    if text.hasPrefix("#") { text.removeFirst() }
    guard text.count == 6, let value = UInt32(text, radix: 16) else { fail("bad colour \(hex)") }
    return NSColor(srgbRed: CGFloat((value >> 16) & 0xff) / 255, green: CGFloat((value >> 8) & 0xff) / 255, blue: CGFloat(value & 0xff) / 255, alpha: 1)
}

var args = Array(CommandLine.arguments.dropFirst())
var glyphColor = "#7fb0ff", tileTop = "#232833", tileBottom = "#0d0f12"
var iconset: String?
var glyphScale: CGFloat = 0.84
var png: (size: Int, path: String)?
var positional: [String] = []
while !args.isEmpty {
    let arg = args.removeFirst()
    func value() -> String {
        if args.isEmpty { fail("\(arg) needs a value") }
        return args.removeFirst()
    }
    switch arg {
    case "--glyph-color": glyphColor = value()
    case "--tile-top": tileTop = value()
    case "--tile-bottom": tileBottom = value()
    case "--iconset": iconset = value()
    case "--glyph-scale":
        guard let scale = Double(value()), scale > 0, scale <= 1 else { fail("bad --glyph-scale") }
        glyphScale = CGFloat(scale)
    case "--png":
        guard let size = Int(value()) else { fail("bad --png size") }
        png = (size, value())
    default: positional.append(arg)
    }
}
guard positional.count == 1, iconset != nil || png != nil else {
    fail("usage: make-icon <glyph.svg> (--iconset <dir> | --png <size> <file>) [--glyph-color #hex] [--tile-top #hex] [--tile-bottom #hex]")
}
guard var svg = try? String(contentsOfFile: positional[0], encoding: .utf8) else { fail("cannot read \(positional[0])") }
svg = svg.replacingOccurrences(of: "fill=\"#000\"", with: "fill=\"\(glyphColor)\"")
guard let glyph = NSImage(data: svg.data(using: .utf8)!) else { fail("NSImage could not decode the SVG") }

func render(canvas: Int, fullBleed: Bool) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: canvas, pixelsHigh: canvas, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: canvas, height: canvas)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSGraphicsContext.current?.imageInterpolation = .high
    let size = CGFloat(canvas)
    let tile = fullBleed ? size : size * 0.824
    let tileRect = NSRect(x: (size - tile) / 2, y: (size - tile) / 2, width: tile, height: tile)
    let path = fullBleed ? NSBezierPath(rect: tileRect) : NSBezierPath(roundedRect: tileRect, xRadius: tile * 0.224, yRadius: tile * 0.224)
    if !fullBleed {
        let shadow = NSShadow()
        shadow.shadowBlurRadius = size * 0.014
        shadow.shadowOffset = NSSize(width: 0, height: -size * 0.006)
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
        shadow.set()
        color(tileBottom).setFill()
        path.fill()
        NSShadow().set()
    }
    NSGradient(starting: color(tileTop), ending: color(tileBottom))!.draw(in: path, angle: -90)
    if !fullBleed {
        // A hairline rim, so the dark tile keeps its edge on a dark Dock.
        NSColor.white.withAlphaComponent(0.10).setStroke()
        let rim = NSBezierPath(roundedRect: tileRect.insetBy(dx: size * 0.002, dy: size * 0.002), xRadius: tile * 0.222, yRadius: tile * 0.222)
        rim.lineWidth = max(1, size * 0.003)
        rim.stroke()
    }
    let side = tile * glyphScale
    glyph.draw(in: NSRect(x: (size - side) / 2, y: (size - side) / 2, width: side, height: side), from: .zero, operation: .sourceOver, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    return rep
}

func write(_ rep: NSBitmapImageRep, to path: String) {
    guard let data = rep.representation(using: .png, properties: [:]) else { fail("png encode failed") }
    do { try data.write(to: URL(fileURLWithPath: path)) } catch { fail("write \(path): \(error)") }
}

if let dir = iconset {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    let sizes: [(String, Int)] = [
        ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
        ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512),
        ("icon_512x512", 512), ("icon_512x512@2x", 1024),
    ]
    for (name, pixels) in sizes { write(render(canvas: pixels, fullBleed: false), to: (dir as NSString).appendingPathComponent(name + ".png")) }
}
if let png { write(render(canvas: png.size, fullBleed: true), to: png.path) }
