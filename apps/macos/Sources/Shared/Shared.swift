// Shared by Papyrus.app and Papyrus Server.app: the configuration build.py
// writes into each bundle, the run logs, the probe that tells whether the
// papyrus web server answers, and the glyph.
//
// Every variant (release, personal, …) is a pair of apps built from these
// sources; papyrus-app.json says which variant a bundle is. Everything the
// apps keep in ~/Library is named by the variant name
// `io.github.taliesinb.papyrus.<variant>` (also the app's bundle id).

import Cocoa

/// `Contents/Resources/papyrus-app.json`, written by build.py. Both apps of a variant carry the same file.
struct PapyrusConfig: Decodable {
    /// The variant id (`release`, `personal`); its library is `~/.papyrus/<variant>`.
    var variant: String = "release"
    /// Display names, also the bundles' file names: `Papyrus` / `Papyrus Server`, `Papyrus Personal` / `Papyrus Personal Server`.
    var name: String = "Papyrus"
    var serverName: String = "Papyrus Server"
    /// The app's URL scheme: `papyrus`, `papyrus-personal`.
    var scheme: String = "papyrus"
    /// Loopback port of the web server.
    var port: Int = 5182
    /// The path the app is served under (SvelteKit `paths.base`), also the `tailscale serve` mount.
    var base: String = "/papyrus"
    var appBundleId: String = "io.github.taliesinb.papyrus.release"
    var serverBundleId: String = "io.github.taliesinb.papyrus.release.server"
    /// Absolute paths found when the bundle was built: a GUI app has no shell PATH.
    var node: String?
    var tailscale: String?
    /// Extra environment for the web server (PAPYRUS_HOME, PAPYRUS_DB, …).
    var env: [String: String]?

    static func load() -> PapyrusConfig {
        guard let url = Bundle.main.url(forResource: "papyrus-app", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return PapyrusConfig() }
        do { return try JSONDecoder().decode(PapyrusConfig.self, from: data) } catch {
            NSLog("papyrus: bad papyrus-app.json: \(error)")
            return PapyrusConfig()
        }
    }

    /// `http://127.0.0.1:<port>/papyrus/`
    var localURL: URL { URL(string: "http://127.0.0.1:\(port)\(base)/")! }
    var versionURL: URL { URL(string: "api/version", relativeTo: localURL)!.absoluteURL }

    /// `io.github.taliesinb.papyrus.<variant>`: names this variant's directories in ~/Library.
    var variantName: String { "io.github.taliesinb.papyrus.\(variant)" }
    /// What the menu-bar scroll carries: nothing for release, else the variant's initial (`P`).
    var initial: String? { variant == "release" ? nil : variant.first.map { String($0).uppercased() } }

    /// `~/Library/<area>/<variant name>/`, created on first use.
    func libraryDirectory(_ area: String) -> URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/\(area)/\(variantName)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
}

extension PapyrusConfig {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = PapyrusConfig()
        variant = try c.decodeIfPresent(String.self, forKey: .variant) ?? d.variant
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? d.name
        serverName = try c.decodeIfPresent(String.self, forKey: .serverName) ?? d.serverName
        scheme = try c.decodeIfPresent(String.self, forKey: .scheme) ?? d.scheme
        port = try c.decodeIfPresent(Int.self, forKey: .port) ?? d.port
        base = try c.decodeIfPresent(String.self, forKey: .base) ?? d.base
        appBundleId = try c.decodeIfPresent(String.self, forKey: .appBundleId) ?? d.appBundleId
        serverBundleId = try c.decodeIfPresent(String.self, forKey: .serverBundleId) ?? d.serverBundleId
        node = try c.decodeIfPresent(String.self, forKey: .node)
        tailscale = try c.decodeIfPresent(String.self, forKey: .tailscale)
        env = try c.decodeIfPresent([String: String].self, forKey: .env)
    }
    enum CodingKeys: String, CodingKey { case variant, name, serverName, scheme, port, base, appBundleId, serverBundleId, node, tailscale, env }
}

/// What `/api/version` reports: the library's counts and any pipeline run in progress.
struct LibraryStatus: Decodable {
    struct Counts: Decodable { var works: Int; var files: Int; var people: Int; var pages: Int }
    struct Run: Decodable { var id: Int; var command: String; var backend: String? }
    var counts: Counts
    var running: [Run]
    var version: String
}

/// Ask the server for its status. nil: nothing answers, or what answers is not papyrus.
func probeLibrary(_ config: PapyrusConfig, timeout: TimeInterval = 3, completion: @escaping (LibraryStatus?) -> Void) {
    var request = URLRequest(url: config.versionURL)
    request.timeoutInterval = timeout
    request.cachePolicy = .reloadIgnoringLocalCacheData
    let session = URLSession(configuration: .ephemeral)
    session.dataTask(with: request) { data, response, _ in
        var status: LibraryStatus?
        if (response as? HTTPURLResponse)?.statusCode == 200, let data {
            status = try? JSONDecoder().decode(LibraryStatus.self, from: data)
        }
        DispatchQueue.main.async { completion(status) }
        session.finishTasksAndInvalidate()
    }.resume()
}

/// `~/Library/Logs/<variant name>/<name>.log`, appended to, one timestamped line per call.
final class RunLog {
    let url: URL
    private let handle: FileHandle?
    private let formatter = ISO8601DateFormatter()

    init(name: String, config: PapyrusConfig) {
        url = config.libraryDirectory("Logs").appendingPathComponent("\(name).log")
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        handle = try? FileHandle(forWritingTo: url)
        handle?.seekToEndOfFile()
    }

    func line(_ text: String) {
        let stamped = "\(formatter.string(from: Date())) \(text)\n"
        handle?.write(stamped.data(using: .utf8)!)
    }

    /// The raw handle, for a child process's stdout and stderr.
    var fileHandle: FileHandle? { handle }
}

/// The menu-bar image: the scroll glyph (boxicons "scroll", MIT) from the bundle as a template image,
/// cropped to the mark so that it spans `height` points (as tall as its neighbours' marks), with
/// `initial` (a variant's first letter) set inside the scroll's paper. Release and the variants
/// draw the scroll at the same size.
func menuBarImage(height: CGFloat = 18, initial: String?, glyph url: URL? = Bundle.main.url(forResource: "glyph", withExtension: "svg")) -> NSImage? {
    guard let url, let glyph = NSImage(contentsOf: url) else { return nil }
    glyph.size = NSSize(width: 24, height: 24)
    // The mark spans 2…22 of the 24-unit viewBox on both axes.
    let unit = height / 20
    let image = NSImage(size: NSSize(width: height, height: height), flipped: false) { rect in
        glyph.draw(in: rect, from: NSRect(x: 2, y: 2, width: 20, height: 20), operation: .sourceOver, fraction: 1)
        guard let initial, !initial.isEmpty else { return true }
        // The paper: x 8…18, y 4…16 of the viewBox (y down), so x 6…16 and y 6…18 up from the mark's corner.
        let paper = NSRect(x: 6 * unit, y: 6 * unit, width: 10 * unit, height: 12 * unit)
        var font = NSFont.systemFont(ofSize: 10 * unit, weight: .heavy)
        var size = (initial as NSString).size(withAttributes: [.font: font])
        let room = paper.width - 2 * unit
        if size.width > room {
            font = NSFont.systemFont(ofSize: font.pointSize * room / size.width, weight: .heavy)
            size = (initial as NSString).size(withAttributes: [.font: font])
        }
        // Centre the capital on the paper: its baseline sits half a cap height below the middle.
        let baseline = paper.midY - font.capHeight / 2
        (initial as NSString).draw(at: NSPoint(x: paper.midX - size.width / 2, y: baseline + font.descender),
                                   withAttributes: [.font: font, .foregroundColor: NSColor.black])
        return true
    }
    image.isTemplate = true
    image.accessibilityDescription = initial.map { "Papyrus \($0)" } ?? "Papyrus"
    return image
}

func escapedHTML(_ value: String) -> String {
    value.replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
        .replacingOccurrences(of: "\"", with: "&quot;")
}
