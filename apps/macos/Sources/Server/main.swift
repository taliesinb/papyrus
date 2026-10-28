// Papyrus Server.app: a menu-bar app (no Dock tile) that runs the read-only
// papyrus web server and publishes it on the tailnet.
//
//   - runs `node <bundle>/Contents/Resources/web/build/index.js` on
//     127.0.0.1:<port> (papyrus-app.json; the app is served under /papyrus),
//     its output in ~/Library/Logs/Papyrus/server.log, restarting it when it
//     exits unexpectedly (with backoff; five quick failures in a row stop it);
//   - keeps its pid in ~/Library/Application Support/Papyrus/server.pid, so a
//     server left behind by a crashed app is stopped before a new one starts;
//     a papyrus server on the port that it did not start (`pnpm start`) is used
//     as it is;
//   - publishes it with `tailscale serve --https=443 --set-path=/papyrus
//     http://127.0.0.1:<port>/papyrus` (Publish on Tailnet, on by default) and
//     withdraws that path when it quits;
//   - the menu shows the library's counts and any pipeline run in progress
//     (`/api/version`), and opens Papyrus.app, the browser, the log;
//   - Open at Login through SMAppService.
//
// Being the parent of the server, this bundle is the process macOS asks the
// user about when the server reads PDFs in ~/Downloads or ~/Documents
// ("Papyrus Server would like to access…"); its Info.plist carries the usage
// strings (build.py).

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
    let log = RunLog(name: "server")
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

    var publishEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "publishOnTailnet") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "publishOnTailnet") }
    }

    var supportDir: URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Papyrus", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
    var pidFile: URL { supportDir.appendingPathComponent("server.pid") }
    var script: URL { Bundle.main.resourceURL!.appendingPathComponent("web/build/index.js") }
    var tailnetURL: URL? { tailnetHost.map { URL(string: "https://\($0)\(config.base)/")! } }

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .filter { $0.processIdentifier != getpid() }
        if !others.isEmpty {
            log.line("another Papyrus Server is running (pid \(others[0].processIdentifier)); exiting")
            exit(0)
        }
        let info = Bundle.main.infoDictionary ?? [:]
        log.line("launch: \(Bundle.main.bundlePath) \(info["CFBundleShortVersionString"] as? String ?? "?") (\(info["CFBundleVersion"] as? String ?? "?")) port \(config.port)")

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = glyphImage(pointSize: 17) ?? NSImage(systemSymbolName: "scroll", accessibilityDescription: "Papyrus")
        statusItem.button?.toolTip = "Papyrus Server"
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        refreshIcon()

        installTerminationSignals()
        start()
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
        stopServer()
        if published {
            // Withdraw /papyrus from the tailnet; tailscaled keeps --bg handlers otherwise.
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
        if let pdftoppm = config.pdftoppm { env["PDFTOPPM"] = pdftoppm }
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

    func poll() {
        guard process != nil || state != .stopped else { return }
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
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let header = menu.addItem(withTitle: "Papyrus Server", action: nil, keyEquivalent: "")
        header.isEnabled = false
        header.attributedTitle = NSAttributedString(string: "Papyrus Server", attributes: [.font: NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)])

        func info(_ text: String) {
            let item = menu.addItem(withTitle: text, action: nil, keyEquivalent: "")
            item.isEnabled = false
        }
        switch state {
        case .stopped: info("Stopped")
        case .starting: info("Starting…")
        case .failed(let why): info("Not running: \(why)")
        case .running(let owned):
            info("Running on 127.0.0.1:\(config.port)\(owned ? "" : " (started elsewhere)")")
            if let c = library?.counts {
                info("\(c.works) works · \(c.files) files · \(c.people) people")
            }
            for run in library?.running ?? [] {
                info("\(run.command) running\(run.backend.map { " (\($0))" } ?? "")")
            }
        }
        if published, let host = tailnetHost {
            info("Published at \(host)\(config.base)")
        } else if publishEnabled, let note = tailscaleNote {
            info("Not on the tailnet: \(note)")
        }

        menu.addItem(.separator())
        add(menu, "Open Papyrus", #selector(openPapyrus), "o")
        add(menu, "Open in Browser", #selector(openInBrowser), "", enabled: isRunning)
        add(menu, "Open Tailnet Address", #selector(openTailnet), "", enabled: published && isRunning)
        add(menu, "Copy Tailnet Address", #selector(copyTailnet), "", enabled: published)

        menu.addItem(.separator())
        add(menu, "Publish on Tailnet", #selector(togglePublish), "").state = publishEnabled ? .on : .off
        add(menu, "Restart Server", #selector(restartServer), "r")
        add(menu, "Show Server Log", #selector(showLog), "l")

        menu.addItem(.separator())
        add(menu, "Open at Login", #selector(toggleLogin), "").state = SMAppService.mainApp.status == .enabled ? .on : .off
        add(menu, "Quit Papyrus Server", #selector(NSApplication.terminate(_:)), "q", target: NSApp)

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

    /// Papyrus.app: the one beside this bundle (a build in dist/), else wherever LaunchServices knows it.
    var papyrusAppURL: URL? {
        let sibling = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("Papyrus.app")
        if FileManager.default.fileExists(atPath: sibling.path) { return sibling }
        return NSWorkspace.shared.urlForApplication(withBundleIdentifier: config.appBundleId)
    }

    @objc func openPapyrus() {
        guard let url = papyrusAppURL else { openInBrowser(); return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
    @objc func openInBrowser() { NSWorkspace.shared.open(config.localURL) }
    @objc func openTailnet() { if let url = tailnetURL { NSWorkspace.shared.open(url) } }
    @objc func copyTailnet() {
        guard let url = tailnetURL else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url.absoluteString, forType: .string)
    }
    @objc func showLog() { NSWorkspace.shared.open(log.url) }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch {
            log.line("open at login: \(error)")
            let alert = NSAlert()
            alert.messageText = "Could not change Open at Login"
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
