// papyrus-thumb: render one page of a PDF to a PNG with CoreGraphics, the macOS
// stand-in for poppler's pdftoppm (which the web server uses on Linux).
//
//   papyrus-thumb <pdf> <page, 1-based> <width px> <out.png>
//
// The page's crop box, turned by its /Rotate, scaled to <width> pixels wide
// (the height follows the aspect ratio), drawn on white in sRGB. Exits 0 once
// <out.png> is written; 1 with a message on stderr otherwise (2 for bad
// arguments). Papyrus Server.app carries it in Contents/MacOS and hands its
// path to the web server as PAPYRUS_THUMB (web/src/lib/server/files.ts).

import CoreGraphics
import Foundation
import ImageIO

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("papyrus-thumb: \(message)\n".utf8))
    exit(code)
}

let args = CommandLine.arguments
guard args.count == 5, let pageNumber = Int(args[2]), pageNumber >= 1,
      let width = Int(args[3]), (1...8192).contains(width) else {
    fail("usage: papyrus-thumb <pdf> <page (1-based)> <width px, 1-8192> <out.png>", code: 2)
}
let input = URL(fileURLWithPath: args[1])
let output = URL(fileURLWithPath: args[4])

guard let doc = CGPDFDocument(input as CFURL) else { fail("cannot open \(input.path) as a PDF") }
// Many PDFs are "encrypted" with an empty user password (permissions only); those open with "".
if doc.isEncrypted && !doc.isUnlocked && !doc.unlockWithPassword("") { fail("\(input.path) is password-protected") }
guard pageNumber <= doc.numberOfPages else { fail("\(input.path) has \(doc.numberOfPages) pages, not \(pageNumber)") }
guard let page = doc.page(at: pageNumber) else { fail("cannot read page \(pageNumber) of \(input.path)") }

let box = page.getBoxRect(.cropBox)
let quarterTurn = page.rotationAngle % 180 != 0
let size = quarterTurn ? CGSize(width: box.height, height: box.width) : box.size
guard size.width > 0, size.height > 0 else { fail("page \(pageNumber) of \(input.path) is empty") }
let scale = CGFloat(width) / size.width
// A strip of a page (a receipt, a scroll) would otherwise ask for an unbounded bitmap.
let height = min(Int((size.height * scale).rounded()), width * 16)

guard height > 0,
      let space = CGColorSpace(name: CGColorSpace.sRGB),
      let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                          space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
else { fail("cannot make a \(width)×\(height) bitmap") }
ctx.setFillColor(red: 1, green: 1, blue: 1, alpha: 1)
ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
ctx.interpolationQuality = .high
ctx.setRenderingIntent(.defaultIntent)
// Height clipped above: keep the top of the page (CoreGraphics' origin is bottom-left).
ctx.translateBy(x: 0, y: CGFloat(height) - size.height * scale)
ctx.scaleBy(x: scale, y: scale)
// getDrawingTransform maps the crop box (and its rotation) onto a rect of the page's own size;
// it never scales up, which is why the scale is applied to the context instead.
ctx.concatenate(page.getDrawingTransform(.cropBox, rect: CGRect(origin: .zero, size: size), rotate: 0, preserveAspectRatio: true))
ctx.drawPDFPage(page)

guard let image = ctx.makeImage() else { fail("rendering page \(pageNumber) failed") }
guard let dest = CGImageDestinationCreateWithURL(output as CFURL, "public.png" as CFString, 1, nil) else {
    fail("cannot write \(output.path)")
}
CGImageDestinationAddImage(dest, image, nil)
guard CGImageDestinationFinalize(dest) else { fail("cannot write \(output.path)") }
