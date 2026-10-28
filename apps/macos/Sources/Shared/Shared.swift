// Shared by Papyrus.app and Papyrus Server.app: the configuration build.py
// writes into each bundle, the run logs, the probe that tells whether the
// papyrus web server answers, and the glyph.

import Cocoa

/// `Contents/Resources/papyrus-app.json`, written by build.py. Both apps carry the same file.
struct PapyrusConfig: Decodable {
    /// Loopback port of the web server.
    var port: Int = 5182
    /// The path the app is served under (SvelteKit `paths.base`), also the `tailscale serve` mount.
    var base: String = "/papyrus"
    var appBundleId: String = "io.github.taliesinb.papyrus"
    var serverBundleId: String = "io.github.taliesinb.papyrus.server"
    /// Absolute paths found when the bundle was built: a GUI app has no shell PATH.
    var node: String?
    var tailscale: String?
    var pdftoppm: String?
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
}

extension PapyrusConfig {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = PapyrusConfig()
        port = try c.decodeIfPresent(Int.self, forKey: .port) ?? d.port
        base = try c.decodeIfPresent(String.self, forKey: .base) ?? d.base
        appBundleId = try c.decodeIfPresent(String.self, forKey: .appBundleId) ?? d.appBundleId
        serverBundleId = try c.decodeIfPresent(String.self, forKey: .serverBundleId) ?? d.serverBundleId
        node = try c.decodeIfPresent(String.self, forKey: .node)
        tailscale = try c.decodeIfPresent(String.self, forKey: .tailscale)
        pdftoppm = try c.decodeIfPresent(String.self, forKey: .pdftoppm)
        env = try c.decodeIfPresent([String: String].self, forKey: .env)
    }
    enum CodingKeys: String, CodingKey { case port, base, appBundleId, serverBundleId, node, tailscale, pdftoppm, env }
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

/// `~/Library/Logs/Papyrus/<name>.log`, appended to, one timestamped line per call.
final class RunLog {
    let url: URL
    private let handle: FileHandle?
    private let formatter = ISO8601DateFormatter()

    init(name: String) {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Papyrus", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        url = dir.appendingPathComponent("\(name).log")
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

/// The scroll glyph (boxicons "scroll", MIT) from the bundle, as a menu-bar template image.
func glyphImage(pointSize: CGFloat) -> NSImage? {
    guard let url = Bundle.main.url(forResource: "glyph", withExtension: "svg"),
          let image = NSImage(contentsOf: url) else { return nil }
    image.size = NSSize(width: pointSize, height: pointSize)
    image.isTemplate = true
    return image
}

func escapedHTML(_ value: String) -> String {
    value.replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
        .replacingOccurrences(of: "\"", with: "&quot;")
}
