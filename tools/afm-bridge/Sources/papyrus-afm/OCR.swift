// OCR for PDF pages without a text layer: render with CoreGraphics, recognise with Vision.
//
//   {"op":"ocr", "path":str, "pages":[int] (1-based), "scale":float? (default 2, i.e. 144 dpi),
//    "max_side":int? (default 4096 px), "languages":[str]? (default: detect),
//    "mode":"paragraphs"|"text"|"lines"? (default paragraphs)}
//     -> {"ok":true, "pages":[{"page":n, "text":str, "confidence":float} | {"page":n, "error":str}],
//         "elapsed_ms":int}
//
// An empty request ({"op":"ocr","path":"","pages":[]}) answers {"ok":true,"pages":[]}: papyrus uses
// it to check that the bridge is new enough to have this op.
//
// Modes: "paragraphs" and "text" use RecognizeDocumentsRequest (macOS 26), which groups lines into
// paragraphs in reading order, also across columns; "paragraphs" joins them with blank lines,
// "text" takes the document's own transcript. "lines" uses RecognizeTextRequest, one line per
// observation, in Vision's order.

import CoreGraphics
import Foundation
import Vision

struct OCRPage: Sendable {
    let page: Int
    var text: String? = nil
    var confidence: Float = 0
    var error: String? = nil

    var json: JSON {
        var o: JSON = ["page": page]
        if let error { o["error"] = error } else { o["text"] = text ?? ""; o["confidence"] = Double(confidence) }
        return o
    }
}

enum OCR {
    /// The page as an 8-bit grey image on white, honouring the page's /Rotate.
    static func render(_ page: CGPDFPage, scale: CGFloat, maxSide: CGFloat) -> CGImage? {
        let box = page.getBoxRect(.cropBox)
        let quarterTurn = page.rotationAngle % 180 != 0
        let size = quarterTurn ? CGSize(width: box.height, height: box.width) : box.size
        var s = scale
        let longest = max(size.width, size.height) * s
        if longest > maxSide { s *= maxSide / longest }
        let w = Int((size.width * s).rounded()), h = Int((size.height * s).rounded())
        guard w > 0, h > 0,
              let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceGray(),
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else { return nil }
        ctx.setFillColor(gray: 1, alpha: 1)
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        ctx.interpolationQuality = .high
        ctx.scaleBy(x: s, y: s)
        ctx.concatenate(page.getDrawingTransform(.cropBox, rect: CGRect(origin: .zero, size: size),
                                                 rotate: 0, preserveAspectRatio: true))
        ctx.drawPDFPage(page)
        return ctx.makeImage()
    }

    static func recognize(_ image: CGImage, mode: String, languages: [String]) async throws -> (String, Float) {
        let langs = languages.map { Locale.Language(identifier: $0) }
        if mode == "lines" {
            var req = RecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = true
            if langs.isEmpty { req.automaticallyDetectsLanguage = true } else { req.recognitionLanguages = langs }
            let obs = try await req.perform(on: image)
            let lines = obs.compactMap { $0.topCandidates(1).first }
            let conf = lines.isEmpty ? 0 : lines.map(\.confidence).reduce(0, +) / Float(lines.count)
            return (lines.map(\.string).joined(separator: "\n"), conf)
        }
        var req = RecognizeDocumentsRequest()
        req.textRecognitionOptions.useLanguageCorrection = true
        if langs.isEmpty {
            req.textRecognitionOptions.automaticallyDetectLanguage = true
        } else {
            req.textRecognitionOptions.automaticallyDetectLanguage = false
            req.textRecognitionOptions.recognitionLanguages = langs
        }
        let docs = try await req.perform(on: image)
        let text = docs.map { d -> String in
            let paras = d.document.paragraphs.map(\.transcript)
            return mode == "text" || paras.isEmpty ? d.document.text.transcript : paras.joined(separator: "\n\n")
        }.joined(separator: "\n\n")
        return (text, docs.map(\.confidence).max() ?? 0)
    }

    static func run(_ req: JSON) async -> JSON {
        let start = ContinuousClock.now
        let path = req["path"] as? String ?? ""
        let pages = (req["pages"] as? [Any] ?? []).compactMap { ($0 as? NSNumber)?.intValue }
        if pages.isEmpty { return ["ok": true, "pages": [] as [Any]] }
        guard let doc = CGPDFDocument(URL(fileURLWithPath: path) as CFURL) else {
            return ["ok": false, "kind": "other", "error": "cannot open PDF: \(path)"]
        }
        if doc.isEncrypted && !doc.isUnlocked { _ = doc.unlockWithPassword("") }
        let scale = CGFloat((req["scale"] as? NSNumber)?.doubleValue ?? 2.0)
        let maxSide = CGFloat((req["max_side"] as? NSNumber)?.doubleValue ?? 4096)
        let mode = req["mode"] as? String ?? "paragraphs"
        let languages = req["languages"] as? [String] ?? []

        // Render one page at a time (CGPDFDocument is not documented as thread-safe), recognise
        // concurrently: Vision runs on the Neural Engine / GPU, rendering on the CPU.
        let results = await withTaskGroup(of: OCRPage.self) { group in
            for n in pages {
                guard n >= 1, n <= doc.numberOfPages, let page = doc.page(at: n) else {
                    group.addTask { OCRPage(page: n, error: "no page \(n)") }
                    continue
                }
                guard let image = render(page, scale: scale, maxSide: maxSide) else {
                    group.addTask { OCRPage(page: n, error: "could not render page \(n)") }
                    continue
                }
                group.addTask {
                    do {
                        let (text, conf) = try await recognize(image, mode: mode, languages: languages)
                        return OCRPage(page: n, text: text, confidence: conf)
                    } catch {
                        return OCRPage(page: n, error: String(describing: error))
                    }
                }
            }
            var out: [OCRPage] = []
            for await r in group { out.append(r) }
            return out.sorted { $0.page < $1.page }
        }
        return ["ok": true, "pages": results.map(\.json), "elapsed_ms": ms(since: start)]
    }
}
