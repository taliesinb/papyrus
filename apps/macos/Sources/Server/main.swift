// Papyrus Server.app: a menu-bar app (no Dock tile) that runs the read-only
// papyrus web server and publishes it on the tailnet. One per variant
// (`Papyrus Personal Server.app`), each with its own menu-bar scroll.
//
//   - runs `node <bundle>/Contents/Resources/web/build/index.js` on
//     127.0.0.1:<port> with PAPYRUS_VARIANT=<variant> (papyrus-app.json; the
//     app is served under its base, /papyrus or /papyrus-<variant>) and
//     PAPYRUS_THUMB=<bundle>/Contents/MacOS/papyrus-thumb (page thumbnails
//     with CoreGraphics; poppler's pdftoppm is the Linux renderer), its
//     output in ~/Library/Logs/<variant name>/server.log, restarting it when it
//     exits unexpectedly (with backoff; five quick failures in a row stop it);
//   - keeps its pid in ~/Library/Application Support/<variant name>/server.pid,
//     so a server left behind by a crashed app is stopped before a new one
//     starts; a papyrus server on the port that it did not start (`pnpm start`)
//     is used as it is;
//   - publishes it with `tailscale serve --https=443 --set-path=<base>
//     http://127.0.0.1:<port><base>` (the menu's "Serving on <host><base>", on by default) and
//     withdraws that path when it quits;
//   - runs `papyrus watch` (the `watch` command in papyrus-app.json, when the
//     variant watches), which adds new PDFs from ~/Downloads and ~/Desktop and
//     processes them in the background; its output goes to
//     ~/Library/Logs/<variant name>/watch.log. It restarts it when it exits
//     unexpectedly (with backoff; five quick failures in a row stop it) and stops
//     it with SIGTERM on quit. Exit status 75 means another watcher holds the
//     library's lock: tried again a minute later. the watcher section's Stop / Start turns it
//     off and on;
//   - the menu shows the library's counts, any pipeline run in progress and the
//     watcher's state (`/api/version`), and opens Papyrus.app, the browser, the logs;
//   - Start at Login through SMAppService.
//   - The menu has a section for the server and one for the watcher (a bold title, the state, the
//     commands: the server's Stop / Start stops or starts the web server and leaves this app
//     running), then Start at Login and Quit.
//
// Being the parent of the server and the watcher, this bundle is the process
// macOS asks the user about when they read PDFs in ~/Downloads, ~/Desktop or
// ~/Documents ("Papyrus Server would like to access…"); its Info.plist carries
// the usage strings (build.py).

import Cocoa
import ServiceManagement

enum ServerState: Equatable {
    case stopped
    case starting
    case running(owned: Bool)
    case failed(String)
}

final class ServerDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let config = PapyrusConfig.load()
    lazy var log = RunLog(name: "server", config: config)
    var statusItem: NSStatusItem!
    let menu = NSMenu()

    var state: ServerState = .stopped { didSet { refreshIcon() } }
    var library: LibraryStatus?
    var process: Process?
    var stopping = false
    var failures: [Date] = []
    var pollTimer: Timer?

    var tailnetHost: String?
    var published = false
    var tailscaleNote: String?

    lazy var watchLog = RunLog(name: "watch", config: config)
    /// The scroll; its paper is solid while the watcher's queue has work.
    lazy var emptyIcon = menuBarImage(initial: config.initial) ?? NSImage(systemSymbolName: "scroll", accessibilityDescription: config.name)
    lazy var filledIcon = menuBarImage(initial: config.initial, filled: true) ?? emptyIcon
    var watcher: Process?
    var watcherStopping = false
    var watcherFailures: [Date] = []
    var watcherNote: String?

    var watchEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "watchForPDFs") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "watchForPDFs") }
    }

    var publishEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "publishOnTailnet") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "publishOnTailnet") }
    }

    var pidFile: URL { config.libraryDirectory("Application Support").appendingPathComponent("server.pid") }
    var script: URL { Bundle.main.resourceURL!.appendingPathComponent("web/build/index.js") }
    var tailnetURL: URL? { tailnetHost.map { URL(string: "https://\($0)\(config.base)/")! } }

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .filter { $0.processIdentifier != getpid() }
        if !others.isEmpty {
            log.line("another \(config.serverName) is running (pid \(others[0].processIdentifier)); exiting")
            exit(0)
        }
        let info = Bundle.main.infoDictionary ?? [:]
        log.line("launch: \(Bundle.main.bundlePath) \(info["CFBundleShortVersionString"] as? String ?? "?") (\(info["CFBundleVersion"] as? String ?? "?")) variant \(config.variant) port \(config.port)")

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = emptyIcon
        statusItem.button?.toolTip = config.serverName
        // Each variant's item keeps its own place in the menu bar.
        statusItem.autosaveName = config.variantName
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        refreshIcon()

        installTerminationSignals()
        start()
        startWatcher()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
    }

    var signalSources: [DispatchSourceSignal] = []

    /// SIGTERM / SIGINT (`kill`, build.py --install, launchd) quit like Quit does: stop the web server
    /// and withdraw the tailscale path, instead of dying and leaving both behind.
    func installTerminationSignals() {
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in
                self?.log.line("signal \(number): quitting")
                NSApp.terminate(nil)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        pollTimer?.invalidate()
        stopWatcher()
        stopServer()
        if published {
            // Withdraw our path from the tailnet; tailscaled keeps --bg handlers otherwise.
            runTailscale(["serve", "--https=443", "--set-path=\(config.base)", "off"])
            log.line("tailscale: withdrew \(config.base)")
        }
        log.line("quit")
        return .terminateNow
    }

    // MARK: the web server

    func start() {
        guard process == nil else { return }
        state = .starting
        stopStaleServer()
        // A papyrus server already on the port (e.g. `pnpm start` in web/): use it.
        probeLibrary(config, timeout: 1.5) { [weak self] status in
            guard let self else { return }
            if let status {
                self.log.line("a papyrus server already answers on port \(self.config.port); using it")
                self.library = status
                self.state = .running(owned: false)
                self.afterRunning()
            } else {
                self.spawn()
            }
        }
    }

    /// Stop a server this app started in an earlier run that did not get to stop it (a crash, a kill -9).
    func stopStaleServer() {
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = Int32(text.split(separator: "\n").first ?? "") else { return }
        try? FileManager.default.removeItem(at: pidFile)
        guard kill(pid, 0) == 0, commandLine(of: pid).contains("web/build/index.js") else { return }
        log.line("stopping a server left from an earlier run (pid \(pid))")
        kill(pid, SIGTERM)
        for _ in 0..<30 where kill(pid, 0) == 0 { usleep(100_000) }
        if kill(pid, 0) == 0 { kill(pid, SIGKILL) }
    }

    func commandLine(of pid: Int32) -> String {
        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-o", "command=", "-p", String(pid)]
        let pipe = Pipe()
        ps.standardOutput = pipe
        ps.standardError = FileHandle.nullDevice
        do { try ps.run() } catch { return "" }
        ps.waitUntilExit()
        return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    }

    func spawn() {
        let node = config.node ?? "/opt/homebrew/bin/node"
        guard FileManager.default.isExecutableFile(atPath: node) else {
            state = .failed("node not found at \(node)")
            log.line("cannot start: no node at \(node) (rebuild with build.py, which records node's path)")
            return
        }
        guard FileManager.default.fileExists(atPath: script.path) else {
            state = .failed("the bundle has no web build")
            log.line("cannot start: no \(script.path)")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: node)
        p.arguments = [script.path]
        p.currentDirectoryURL = script.deletingLastPathComponent().deletingLastPathComponent()
        var env = ProcessInfo.processInfo.environment
        env["PORT"] = String(config.port)
        env["HOST"] = "127.0.0.1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["NODE_ENV"] = "production"
        env["PAPYRUS_VARIANT"] = config.variant
        let thumb = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/papyrus-thumb").path
        if FileManager.default.isExecutableFile(atPath: thumb) { env["PAPYRUS_THUMB"] = thumb }
        for (key, value) in config.env ?? [:] { env[key] = value }
        p.environment = env
        p.standardOutput = log.fileHandle
        p.standardError = log.fileHandle
        p.standardInput = FileHandle.nullDevice
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.serverExited(proc) }
        }
        do {
            try p.run()
        } catch {
            state = .failed("cannot start node: \(error.localizedDescription)")
            log.line("cannot start \(node): \(error)")
            return
        }
        process = p
        try? "\(p.processIdentifier)\n\(script.path)\n".write(to: pidFile, atomically: true, encoding: .utf8)
        log.line("started the web server: pid \(p.processIdentifier), \(config.localURL.absoluteString)")
        waitUntilAnswering(deadline: Date().addingTimeInterval(30))
    }

    func waitUntilAnswering(deadline: Date) {
        probeLibrary(config, timeout: 1) { [weak self] status in
            guard let self, self.process != nil, self.state == .starting else { return }
            if let status {
                self.library = status
                self.state = .running(owned: true)
                self.log.line("the web server answers")
                self.afterRunning()
            } else if Date() < deadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitUntilAnswering(deadline: deadline) }
            } else {
                self.log.line("the web server did not answer within 30 s")
                self.state = .failed("the web server does not answer")
            }
        }
    }

    func serverExited(_ proc: Process) {
        guard proc === process else { return }
        process = nil
        try? FileManager.default.removeItem(at: pidFile)
        let how = proc.terminationReason == .uncaughtSignal ? "signal \(proc.terminationStatus)" : "status \(proc.terminationStatus)"
        log.line("the web server exited (\(how))")
        if stopping { stopping = false; state = .stopped; return }
        let now = Date()
        failures = failures.filter { now.timeIntervalSince($0) < 60 } + [now]
        if failures.count >= 5 {
            state = .failed("the web server keeps exiting (\(how)); see the log")
            return
        }
        let delay = pow(2.0, Double(failures.count - 1))
        state = .starting
        log.line("restarting in \(Int(delay)) s")
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.process == nil, !self.stopping else { return }
            self.spawn()
        }
    }

    func stopServer() {
        guard let p = process else { return }
        stopping = true
        p.terminate()
        for _ in 0..<30 where p.isRunning { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
        try? FileManager.default.removeItem(at: pidFile)
        log.line("stopped the web server")
    }

    @objc func restartServer() {
        failures = []
        if case .running(owned: false) = state {
            log.line("restart: the server on the port is not ours; probing again")
        }
        stopServer()
        stopping = false
        state = .stopped
        start()
    }

    /// Stop: the web server stops (and our tailnet path is withdrawn, Serving on the tailnet stays
    /// ticked), this app keeps running; Start: it starts again (and is published again).
    @objc func toggleServer() {
        failures = []
        if state == .stopped {
            log.line("started from the menu")
            start()
            return
        }
        if case .running(owned: false) = state {
            log.line("stop: the server on the port is not ours; no longer using it")
        }
        stopServer()
        stopping = false
        state = .stopped
        if published { unpublish() }
        log.line("stopped from the menu")
    }

    func poll() {
        guard process != nil || state != .stopped else { refreshIcon(); return }
        probeLibrary(config, timeout: 2) { [weak self] status in
            guard let self else { return }
            self.library = status ?? self.library
            switch self.state {
            case .running(let owned) where status == nil && !owned:
                // The server we were using went away: start our own.
                self.log.line("the external server stopped answering; starting ours")
                self.state = .stopped
                self.start()
            case .failed where status != nil:
                self.state = .running(owned: self.process != nil)
            default: break
            }
            self.refreshIcon()
        }
    }

    func afterRunning() {
        failures = []
        if publishEnabled { publish() } else { refreshTailnet() }
    }

    // MARK: the watcher (papyrus watch)

    func startWatcher() {
        guard watchEnabled, watcher == nil, let command = config.watch, let exe = command.first else { return }
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            watcherNote = "\(exe) not found"
            watchLog.line("cannot start the watcher: no \(exe) (rebuild with build.py)")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = Array(command.dropFirst())
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PAPYRUS_VARIANT"] = config.variant
        env["PYTHONUNBUFFERED"] = "1"
        for (key, value) in config.env ?? [:] { env[key] = value }
        p.environment = env
        p.standardOutput = watchLog.fileHandle
        p.standardError = watchLog.fileHandle
        p.standardInput = FileHandle.nullDevice
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.watcherExited(proc) }
        }
        do {
            try p.run()
        } catch {
            watcherNote = "cannot start: \(error.localizedDescription)"
            watchLog.line("cannot start \(exe): \(error)")
            return
        }
        watcher = p
        watcherNote = nil
        watchLog.line("started the watcher: pid \(p.processIdentifier)")
    }

    func watcherExited(_ proc: Process) {
        guard proc === watcher else { return }
        watcher = nil
        let how = proc.terminationReason == .uncaughtSignal ? "signal \(proc.terminationStatus)" : "status \(proc.terminationStatus)"
        watchLog.line("the watcher exited (\(how))")
        if watcherStopping { watcherStopping = false; return }
        var delay: Double
        if proc.terminationReason == .exit && proc.terminationStatus == 75 {
            // EXIT_LOCKED: another `papyrus watch` runs for this library (e.g. in a terminal).
            watcherNote = "another watcher runs for this library"
            delay = 60
        } else {
            let now = Date()
            watcherFailures = watcherFailures.filter { now.timeIntervalSince($0) < 600 } + [now]
            if watcherFailures.count >= 5 {
                watcherNote = "keeps exiting (\(how)); see the watcher log"
                return
            }
            delay = min(60, pow(2.0, Double(watcherFailures.count)))
            watcherNote = "restarting"
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.watcher == nil, self.watchEnabled else { return }
            self.startWatcher()
        }
    }

    /// SIGTERM: the watcher closes its run as interrupted and unloads any model it loaded.
    func stopWatcher() {
        guard let p = watcher else { return }
        watcherStopping = true
        p.terminate()
        for _ in 0..<100 where p.isRunning { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        watcher = nil
        watchLog.line("stopped the watcher")
    }

    /// The library's home, as the CLI finds it: PAPYRUS_HOME, else $PAPYRUS_ROOT (~/.papyrus)/<variant>.
    var libraryHome: URL {
        let env = ProcessInfo.processInfo.environment.merging(config.env ?? [:]) { _, configured in configured }
        if let home = env["PAPYRUS_HOME"], !home.isEmpty {
            return URL(fileURLWithPath: (home as NSString).expandingTildeInPath)
        }
        let root = env["PAPYRUS_ROOT"].map { ($0 as NSString).expandingTildeInPath }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".papyrus").path
        return URL(fileURLWithPath: root).appendingPathComponent(config.variant)
    }

    /// `<home>/watch.pause`: while it exists, the watcher (ours or one in a terminal) halts its loop:
    /// it adds and ingests nothing, and takes the files that arrived meanwhile on resume
    /// (`papyrus pause` / `papyrus resume`).
    var pauseFile: URL { libraryHome.appendingPathComponent("watch.pause") }
    var ingestionPaused: Bool { FileManager.default.fileExists(atPath: pauseFile.path) }

    @objc func togglePause() {
        if ingestionPaused {
            try? FileManager.default.removeItem(at: pauseFile)
            watchLog.line("ingestion resumed from the menu")
        } else {
            FileManager.default.createFile(atPath: pauseFile.path, contents: nil)
            watchLog.line("ingestion paused from the menu")
        }
        poll()
    }

    /// Stop / Start: whether this app runs the watcher at all (remembered across launches).
    @objc func toggleWatch() {
        watchEnabled.toggle()
        watcherFailures = []
        if watchEnabled { startWatcher() } else { stopWatcher(); watcherNote = nil }
    }

    @objc func restartWatcher() {
        watcherFailures = []
        stopWatcher()
        startWatcher()
    }

    /// The watcher's `<home>/watch.json`, if the process that wrote it is alive (whoever runs it:
    /// ours, or `papyrus watch` in a terminal). Read directly, so the section works while the web
    /// server is stopped.
    var watchStatus: LibraryStatus.Watch? {
        guard let data = try? Data(contentsOf: libraryHome.appendingPathComponent("watch.json")),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let pid = raw["pid"] as? Int32 ?? (raw["pid"] as? Int).map(Int32.init),
              let state = raw["state"] as? String, state != "stopped",
              kill(pid, 0) == 0 || errno == EPERM else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(LibraryStatus.Watch.self, from: data)
    }

    /// The watcher section's lines: "Stopped" or "Paused" first when so, then "Watching for new
    /// files" when there is nothing to do, else "Ingested N/M files" and one line per pipeline stage
    /// with work left. The stages take turns (one loop, local models), so one is active and the
    /// others say "pending" (or why they wait).
    var watchLines: [String] {
        guard config.watch != nil else { return [] }
        let w = watchStatus
        if !watchEnabled && w == nil { return ["Stopped"] }
        var lines: [String] = ingestionPaused ? ["Paused"] : []
        guard let w else {
            if let note = watcherNote, watcher == nil { return lines + [note.prefix(1).uppercased() + note.dropFirst()] }
            return lines + [watcher == nil ? "Not running" : "Starting…"]
        }
        let stages = w.stages ?? []
        if let i = w.ingesting, !stages.isEmpty {
            lines.append("Ingested \(i.done)/\(i.total) \(i.total == 1 ? "file" : "files")")
        } else if stages.isEmpty {
            lines.append("Watching for new files")
        } else {
            lines.append("Ingesting")
        }
        for s in stages {
            let one = s.unit.hasSuffix("s") ? String(s.unit.dropLast()) : s.unit
            var line = "\(s.label): \(s.pending) \(s.pending == 1 ? one : s.unit)"
            switch s.state {
            case "active":
                if let per = s.perItemS {
                    line += " · ~\(clock(per))/\(one)"
                    if let eta = s.etaS { line += " · ~\(clock(eta)) left" }
                } else {
                    line += " · timing…"
                }
            case "waiting": line += " · waiting" + (s.reason.map { ": \($0)" } ?? "")
            case "paused": line += " · paused"
            default: line += " · pending"
            }
            lines.append(line)
        }
        if let failed = w.failed, failed > 0 { lines.append("\(failed) failed (papyrus watch --retry-failed)") }
        if w.state == "idle", !w.detail.isEmpty { lines.append(w.detail) }  // e.g. a folder it may not read
        return lines
    }

    /// 3.2s, 45s, 5m30s, 1h05m.
    func clock(_ seconds: Double) -> String {
        if seconds < 10 { return String(format: "%.1fs", seconds) }
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s)s" }
        if s < 3600 { return String(format: "%dm%02ds", s / 60, s % 60) }
        return String(format: "%dh%02dm", s / 3600, (s % 3600) / 60)
    }

    // MARK: tailscale

    var tailscalePath: String? {
        let candidates = [config.tailscale, "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                          "/Applications/Tailscale.app/Contents/MacOS/Tailscale"].compactMap { $0 }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    @discardableResult
    func runTailscale(_ arguments: [String]) -> (status: Int32, output: String) {
        guard let path = tailscalePath else { return (127, "tailscale CLI not found") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = arguments
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return (127, error.localizedDescription) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    /// This node's MagicDNS name, or a note on why there is none.
    func tailnetName() -> (host: String?, note: String?) {
        let (status, output) = runTailscale(["status", "--json"])
        guard status == 0, let json = try? JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any] else {
            return (nil, tailscalePath == nil ? "Tailscale CLI not found" : "Tailscale is not running")
        }
        guard json["BackendState"] as? String == "Running" else { return (nil, "Tailscale is \(json["BackendState"] as? String ?? "not running")") }
        let name = ((json["Self"] as? [String: Any])?["DNSName"] as? String ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return name.isEmpty ? (nil, "this node has no MagicDNS name") : (name, nil)
    }

    /// Whether `tailscale serve` already proxies our mount to our server.
    func serveTarget(host: String) -> String? {
        let (status, output) = runTailscale(["serve", "status", "--json"])
        guard status == 0, let json = try? JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any],
              let web = json["Web"] as? [String: Any],
              let site = web["\(host):443"] as? [String: Any],
              let handlers = site["Handlers"] as? [String: Any],
              let handler = handlers[config.base] as? [String: Any] else { return nil }
        return handler["Proxy"] as? String
    }

    var serveProxy: String { "http://127.0.0.1:\(config.port)\(config.base)" }

    func publish() {
        DispatchQueue.global(qos: .utility).async { [self] in
            let (host, note) = tailnetName()
            var ok = false
            var message = note
            if let host {
                if serveTarget(host: host) == serveProxy {
                    ok = true
                } else {
                    let (status, output) = runTailscale(["serve", "--bg", "--yes", "--https=443", "--set-path=\(config.base)", serveProxy])
                    ok = status == 0
                    if !ok { message = "tailscale serve failed: \(output.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: "\n").last ?? "")" }
                }
            }
            DispatchQueue.main.async {
                self.tailnetHost = host
                self.published = ok
                self.tailscaleNote = message
                self.log.line(ok ? "tailscale: published at https://\(host ?? "?")\(self.config.base)/" : "tailscale: not published (\(message ?? "?"))")
                self.refreshIcon()
            }
        }
    }

    func unpublish() {
        DispatchQueue.global(qos: .utility).async { [self] in
            runTailscale(["serve", "--https=443", "--set-path=\(config.base)", "off"])
            DispatchQueue.main.async {
                self.published = false
                self.log.line("tailscale: withdrew \(self.config.base)")
            }
        }
    }

    func refreshTailnet() {
        DispatchQueue.global(qos: .utility).async { [self] in
            let (host, note) = tailnetName()
            let target = host.flatMap { serveTarget(host: $0) }
            DispatchQueue.main.async {
                self.tailnetHost = host
                self.tailscaleNote = note
                self.published = target == self.serveProxy
            }
        }
    }

    @objc func togglePublish() {
        publishEnabled.toggle()
        if publishEnabled { publish() } else { unpublish() }
    }

    // MARK: menu

    func refreshIcon() {
        guard let button = statusItem?.button else { return }
        switch state {
        case .running: button.appearsDisabled = false
        default: button.appearsDisabled = true
        }
        let image = queueHasWork ? filledIcon : emptyIcon
        if button.image !== image { button.image = image }
    }

    /// A watcher of this library (ours, or one started in a terminal) has items queued (due, or
    /// waiting for a retry). `/api/version` reports `watch` only while that process lives.
    var queueHasWork: Bool {
        (watchStatus?.pending ?? [:]).contains { $0.key != "failed" && $0.value > 0 }
    }

    /// `~12 min`, `~1 h 20 min`, `under a minute`.
    /// Two sections, the server's and the watcher's, each a bold title, its state, then its commands;
    /// then Start at Login and Quit (which stops both and quits this app).
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        func title(_ text: String) {
            let item = menu.addItem(withTitle: text, action: nil, keyEquivalent: "")
            item.isEnabled = false
            item.attributedTitle = NSAttributedString(string: text, attributes: [.font: NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)])
        }
        func info(_ text: String) {
            let item = menu.addItem(withTitle: text, action: nil, keyEquivalent: "")
            item.isEnabled = false
        }

        // the web server
        title(config.serverName)
        switch state {
        case .stopped: info("Not serving")
        case .starting: info("Starting…")
        case .failed(let why): info("Not serving: \(why)")
        case .running(let owned):
            info("Serving on 127.0.0.1:\(config.port)\(owned ? "" : " (started elsewhere)")")
            if let c = library?.counts {
                info("\(c.works) works · \(c.files) files · \(c.people) people")
            }
            for run in library?.running ?? [] where run.command != "watch" {
                info("\(run.command) running\(run.backend.map { " (\($0))" } ?? "")")
            }
        }
        if let host = tailnetHost {  // only when Tailscale is there
            add(menu, "Serving on \(host)\(config.base)", #selector(togglePublish), "").state = publishEnabled ? .on : .off
            if publishEnabled, !published, isRunning, let note = tailscaleNote { info(note) }
        }
        add(menu, "Open desktop app", #selector(openPapyrus), "o")
        add(menu, "Open browser app", #selector(openInBrowser), "", enabled: isRunning)
        add(menu, "Copy browser app URL", #selector(copyBrowserURL), "")
        add(menu, "Show Log", #selector(showLog), "l")
        add(menu, "Restart", #selector(restartServer), "r", enabled: state != .stopped)
        add(menu, state == .stopped ? "Start" : "Stop", #selector(toggleServer), "")

        // the watcher
        if config.watch != nil {
            menu.addItem(.separator())
            title("\(config.name) Watcher")
            for line in watchLines { info(line) }
            add(menu, "Show Log", #selector(showWatchLog), "")
            add(menu, ingestionPaused ? "Resume" : "Pause", #selector(togglePause), "")
            if watcher != nil { add(menu, "Restart", #selector(restartWatcher), "") }
            add(menu, watchEnabled ? "Stop" : "Start", #selector(toggleWatch), "")
        }

        menu.addItem(.separator())
        add(menu, "Start at Login", #selector(toggleLogin), "").state = SMAppService.mainApp.status == .enabled ? .on : .off
        add(menu, "Quit", #selector(NSApplication.terminate(_:)), "q", target: NSApp)

        poll()
    }

    var isRunning: Bool { if case .running = state { return true } else { return false } }

    @discardableResult
    func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String, enabled: Bool = true, target: AnyObject? = nil) -> NSMenuItem {
        let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
        item.target = target ?? self
        item.isEnabled = enabled
        return item
    }

    /// This variant's Papyrus.app: the one beside this bundle (a build in dist/), else wherever LaunchServices knows it.
    var papyrusAppURL: URL? {
        let sibling = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("\(config.name).app")
        if FileManager.default.fileExists(atPath: sibling.path) { return sibling }
        return NSWorkspace.shared.urlForApplication(withBundleIdentifier: config.appBundleId)
    }

    @objc func openPapyrus() {
        guard let url = papyrusAppURL else { openInBrowser(); return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
    /// The browser app's address: on the tailnet when served there, else on this Mac.
    var browserURL: URL { (published ? tailnetURL : nil) ?? config.localURL }
    @objc func openInBrowser() { NSWorkspace.shared.open(browserURL) }
    @objc func copyBrowserURL() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(browserURL.absoluteString, forType: .string)
    }
    @objc func showLog() { NSWorkspace.shared.open(log.url) }
    @objc func showWatchLog() { NSWorkspace.shared.open(watchLog.url) }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch {
            log.line("open at login: \(error)")
            let alert = NSAlert()
            alert.messageText = "Could not change Start at Login"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }
}

let app = NSApplication.shared
let delegate = ServerDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
