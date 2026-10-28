// Papyrus.app: a native window (WKWebView) around the read-only papyrus web
// browser served by Papyrus Server.app.
//
//   - on launch (and on Reconnect) it asks the server for `/api/version` on
//     127.0.0.1:<port>; when nothing answers it launches Papyrus Server.app
//     (the one beside it, else the one LaunchServices knows), shows a
//     "Starting…" page and loads the library as soon as the server answers;
//   - links that leave the app (other hosts, other paths than /papyrus) open
//     in the default browser; in-app links with target=_blank (a PDF's
//     "Open ↗") open in a second window of its own;
//   - PDF downloads (`?download`) go to ~/Downloads;
//   - `papyrus://<path>` URLs open that path of the app (`open papyrus://work/43`,
//     `papyrus://?q=transformers`);
//   - menus: ⌘F focuses the library search, ⌘[ / ⌘] go back and forward,
//     ⌘R reloads, zoom, Open in Browser (⇧⌘O), Copy Address (⇧⌘C), Papyrus
//     Server's menu-bar app (Show Server);
//   - the run log is ~/Library/Logs/Papyrus/app.log; `kill -USR1 <pid>` saves
//     a snapshot of the key window's page beside it (snapshot.png), for
//     checking the app from a script.

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    let config = PapyrusConfig.load()
    let log = RunLog(name: "app")
    var window: NSWindow!
    var webView: WKWebView!
    var titleObservation: NSKeyValueObservation?
    var popups: [NSWindow] = []
    var popupTitles: [ObjectIdentifier: NSKeyValueObservation] = [:]
    var showingStatusPage = false
    var connecting = false
    var retryTimer: Timer?

    static let background = NSColor(srgbRed: 0x0d / 255, green: 0x0f / 255, blue: 0x12 / 255, alpha: 1)

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        log.line("launch: \(Bundle.main.bundlePath) \(config.localURL.absoluteString)")
        buildMenu()

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.applicationNameForUserAgent = "Papyrus/\(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0")"
        configuration.preferences.isElementFullscreenEnabled = true
        configuration.userContentController.add(self, name: "papyrusApp")

        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsMagnification = true
        webView.underPageBackgroundColor = Self.background
        if #available(macOS 13.3, *) { webView.isInspectable = true }
        webView.autoresizingMask = [.width, .height]

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Papyrus"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = Self.background
        // A title bar in the page's own colour, without a separator: it reads as part of the page.
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 480, height: 360)
        window.contentView = webView
        window.setFrameAutosaveName("papyrus.main")
        if !window.setFrameUsingName("papyrus.main") { window.center() }
        titleObservation = webView.observe(\.title, options: [.new]) { [weak self] view, _ in
            guard let self, !self.showingStatusPage else { return }
            let title = (view.title ?? "").replacingOccurrences(of: " · papyrus", with: "")
            self.window.title = title.isEmpty || title == "papyrus" ? "Papyrus" : title
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        installSnapshotSignal()
        connect()
    }

    var snapshotSignal: DispatchSourceSignal?

    func installSnapshotSignal() {
        signal(SIGUSR1, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        source.setEventHandler { [weak self] in self?.saveSnapshot() }
        source.resume()
        snapshotSignal = source
    }

    func saveSnapshot() {
        let target = log.url.deletingLastPathComponent().appendingPathComponent("snapshot.png")
        activeWebView.takeSnapshot(with: nil) { [weak self] image, error in
            guard let image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else {
                self?.log.line("snapshot failed: \(error?.localizedDescription ?? "no image")")
                return
            }
            try? png.write(to: target)
            self?.log.line("snapshot: \(target.path) (\(self?.activeWebView.url?.absoluteString ?? "-"))")
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// A papyrus:// URL to load once connected (it may arrive before the server answers).
    var pendingURL: URL?

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.last, url.scheme == "papyrus" else { return }
        // papyrus://work/43?q=x → <local>/papyrus/work/43?q=x (the host is the first path segment).
        var path = (url.host.map { "/\($0)" } ?? "") + url.path
        if let query = url.query { path += "?\(query)" }
        guard let target = URL(string: String(path.drop { $0 == "/" }), relativeTo: config.localURL)?.absoluteURL else { return }
        log.line("open \(url.absoluteString) → \(target.absoluteString)")
        window?.makeKeyAndOrderFront(nil)
        if webView == nil || showingStatusPage || connecting { pendingURL = target } else { load(target) }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        if showingStatusPage, !connecting { connect() }
    }

    // MARK: connecting

    /// Load the library when the server answers; otherwise start Papyrus Server.app and wait for it.
    @objc func connect() {
        if connecting { return }
        connecting = true
        retryTimer?.invalidate()
        probeLibrary(config, timeout: 1.5) { [weak self] status in
            guard let self else { return }
            if status != nil {
                self.connecting = false
                self.load(self.config.localURL)
                return
            }
            guard let server = self.serverAppURL else {
                self.connecting = false
                self.showStatus(title: "Papyrus Server is not installed",
                                detail: "Papyrus needs Papyrus Server.app beside it or in Applications. Build both with apps/macos/build.py.",
                                retry: false)
                return
            }
            self.showStatus(title: "Starting Papyrus Server…", detail: "", retry: false, spinner: true)
            self.log.line("the server does not answer; launching \(server.path)")
            let open = NSWorkspace.OpenConfiguration()
            open.activates = false
            open.addsToRecentItems = false
            NSWorkspace.shared.openApplication(at: server, configuration: open) { _, error in
                DispatchQueue.main.async {
                    if let error {
                        self.connecting = false
                        self.log.line("cannot launch the server: \(error)")
                        self.showStatus(title: "Papyrus Server did not start", detail: error.localizedDescription, retry: true)
                        return
                    }
                    self.waitForServer(deadline: Date().addingTimeInterval(40))
                }
            }
        }
    }

    func waitForServer(deadline: Date) {
        probeLibrary(config, timeout: 1) { [weak self] status in
            guard let self else { return }
            if status != nil {
                self.connecting = false
                self.log.line("the server answers")
                self.load(self.config.localURL)
            } else if Date() < deadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.waitForServer(deadline: deadline) }
            } else {
                self.connecting = false
                self.showStatus(title: "Papyrus Server does not answer",
                                detail: "It was started but nothing answers on \(self.config.localURL.absoluteString). Its menu-bar menu has Show Server Log.",
                                retry: true)
            }
        }
    }

    /// Papyrus Server.app: the one beside this bundle (a build in dist/), else wherever LaunchServices knows it.
    var serverAppURL: URL? {
        let sibling = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("Papyrus Server.app")
        if FileManager.default.fileExists(atPath: sibling.path) { return sibling }
        return NSWorkspace.shared.urlForApplication(withBundleIdentifier: config.serverBundleId)
    }

    func load(_ url: URL) {
        showingStatusPage = false
        let url = pendingURL.flatMap { url == config.localURL ? $0 : nil } ?? url
        pendingURL = nil
        webView.load(URLRequest(url: url))
    }

    func showStatus(title: String, detail: String, retry: Bool, spinner: Bool = false) {
        showingStatusPage = true
        window.title = "Papyrus"
        retryTimer?.invalidate()
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><title>Papyrus</title>
        <style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d0f12;color:#d9dee6;
        font:15px/1.5 -apple-system,system-ui,sans-serif}main{width:min(34em,calc(100% - 3em));text-align:center}
        h1{font:500 1.6em 'Iowan Old Style',Charter,Palatino,Georgia,serif;color:#f1f4f8;margin:.4em 0}p{color:#8a93a3}
        code{font:.85em ui-monospace,Menlo,monospace;color:#8a93a3}
        button{font:600 1em -apple-system,system-ui;padding:.6em 1.4em;border-radius:8px;border:1px solid #343a45;background:#1d2128;color:#f1f4f8;cursor:pointer}
        .spin{width:22px;height:22px;margin:0 auto;border:2px solid #343a45;border-top-color:#7fb0ff;border-radius:50%;animation:s .8s linear infinite}
        @keyframes s{to{transform:rotate(360deg)}}</style></head>
        <body><main>\(spinner ? "<div class=spin></div>" : "")<h1>\(escapedHTML(title))</h1>
        \(detail.isEmpty ? "" : "<p>\(escapedHTML(detail))</p>")
        \(retry ? "<p><button autofocus onclick=\"webkit.messageHandlers.papyrusApp.postMessage('retry')\">Try Again</button></p><p style=font-size:.85em>Trying again every 5 seconds.</p>" : "")
        </main></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
        if retry {
            retryTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in self?.connect() }
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.body as? String == "retry" { connect() }
    }

    // MARK: navigation

    /// In the app: the server's origin under the base path.
    func inScope(_ url: URL) -> Bool {
        let local = config.localURL
        guard url.scheme == local.scheme, url.host == local.host, url.port == local.port else { return false }
        return url.path == config.base || url.path.hasPrefix(config.base + "/")
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if ["about", "blob", "data"].contains(url.scheme ?? "") { decisionHandler(.allow); return }
        let mainFrame = navigationAction.targetFrame?.isMainFrame ?? true
        if mainFrame, !inScope(url), ["http", "https", "mailto"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        if navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let http = navigationResponse.response as? HTTPURLResponse,
           let disposition = http.value(forHTTPHeaderField: "Content-Disposition"), disposition.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
            return
        }
        if navigationResponse.isForMainFrame, !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return } // became a download
        log.line("load failed: \(e.localizedDescription)")
        guard webView === self.webView else { return }
        // The server went away (quit from its menu, a restart): start it again.
        connect()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        log.line("web content process terminated; reloading")
        webView.reload()
    }

    /// target=_blank inside the app (a PDF's "Open ↗"): a second window of ours, sharing cookies.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        guard inScope(url) else { NSWorkspace.shared.open(url); return nil }
        let popup = WKWebView(frame: .zero, configuration: configuration)
        popup.navigationDelegate = self
        popup.uiDelegate = self
        popup.underPageBackgroundColor = Self.background
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 1100),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = url.lastPathComponent
        w.appearance = NSAppearance(named: .darkAqua)
        w.contentView = popup
        w.isReleasedWhenClosed = false
        w.tabbingMode = .disallowed
        w.cascadeTopLeft(from: NSPoint(x: window.frame.minX + 40, y: window.frame.maxY - 40))
        popups.append(w)
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w, queue: .main) { [weak self] _ in
            self?.popups.removeAll { $0 === w }
            self?.popupTitles[ObjectIdentifier(w)] = nil
        }
        // The page's own title, once it has one, beats the URL's last component.
        popupTitles[ObjectIdentifier(w)] = popup.observe(\.title, options: [.new]) { view, _ in
            if let t = view.title, !t.isEmpty { w.title = t }
        }
        w.makeKeyAndOrderFront(nil)
        return popup
    }

    func webViewDidClose(_ webView: WKWebView) {
        popups.first { $0.contentView === webView }?.close()
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    // MARK: downloads

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first!
        var target = dir.appendingPathComponent(suggestedFilename)
        let stem = target.deletingPathExtension().lastPathComponent
        let ext = target.pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: target.path) {
            target = dir.appendingPathComponent(ext.isEmpty ? "\(stem) \(n)" : "\(stem) \(n).\(ext)")
            n += 1
        }
        log.line("download: \(target.path)")
        completionHandler(target)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let alert = NSAlert()
        alert.messageText = "Download failed"
        alert.informativeText = error.localizedDescription
        alert.runModal()
    }

    // MARK: menu actions

    /// The web view of the key window: the main one or a PDF window.
    var activeWebView: WKWebView { (NSApp.keyWindow?.contentView as? WKWebView) ?? webView }

    @objc func reload() { if showingStatusPage { connect() } else { activeWebView.reloadFromOrigin() } }
    @objc func goBack() { activeWebView.goBack() }
    @objc func goForward() { activeWebView.goForward() }
    @objc func goHome() { window.makeKeyAndOrderFront(nil); load(config.localURL) }
    @objc func zoomIn() { activeWebView.pageZoom = min(activeWebView.pageZoom + 0.1, 3) }
    @objc func zoomOut() { activeWebView.pageZoom = max(activeWebView.pageZoom - 0.1, 0.5) }
    @objc func zoomReset() { activeWebView.pageZoom = 1 }
    @objc func openInBrowser() { NSWorkspace.shared.open(activeWebView.url.flatMap { inScope($0) ? $0 : nil } ?? config.localURL) }
    @objc func copyAddress() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString((activeWebView.url ?? config.localURL).absoluteString, forType: .string)
    }
    @objc func findInLibrary() {
        window.makeKeyAndOrderFront(nil)
        webView.evaluateJavaScript("(() => { const s = document.querySelector('input[type=search]'); if (!s) return false; s.focus(); s.select(); return true })()")
    }
    @objc func showServer() {
        guard let url = serverAppURL else { NSSound.beep(); return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }

    func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem()
            main.addItem(item)
            let menu = NSMenu(title: title)
            item.submenu = menu
            return menu
        }
        @discardableResult
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = mods
            return item
        }

        let app = submenu("Papyrus")
        add(app, "About Papyrus", #selector(NSApplication.orderFrontStandardAboutPanel(_:)))
        app.addItem(.separator())
        add(app, "Show Papyrus Server", #selector(showServer))
        app.addItem(.separator())
        add(app, "Hide Papyrus", #selector(NSApplication.hide(_:)), "h")
        add(app, "Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        add(app, "Show All", #selector(NSApplication.unhideAllApplications(_:)))
        app.addItem(.separator())
        add(app, "Quit Papyrus", #selector(NSApplication.terminate(_:)), "q")

        let file = submenu("File")
        add(file, "Library", #selector(goHome), "l", [.command, .shift])
        add(file, "Find in Library", #selector(findInLibrary), "f")
        file.addItem(.separator())
        add(file, "Close Window", #selector(NSWindow.performClose(_:)), "w")

        let edit = submenu("Edit")
        add(edit, "Undo", Selector(("undo:")), "z")
        add(edit, "Redo", Selector(("redo:")), "z", [.command, .shift])
        edit.addItem(.separator())
        add(edit, "Cut", #selector(NSText.cut(_:)), "x")
        add(edit, "Copy", #selector(NSText.copy(_:)), "c")
        add(edit, "Paste", #selector(NSText.paste(_:)), "v")
        add(edit, "Select All", #selector(NSText.selectAll(_:)), "a")

        let view = submenu("View")
        add(view, "Reload", #selector(reload), "r")
        add(view, "Reconnect", #selector(connect))
        view.addItem(.separator())
        add(view, "Back", #selector(goBack), "[")
        add(view, "Forward", #selector(goForward), "]")
        view.addItem(.separator())
        add(view, "Actual Size", #selector(zoomReset), "0")
        add(view, "Zoom In", #selector(zoomIn), "+")
        add(view, "Zoom Out", #selector(zoomOut), "-")
        view.addItem(.separator())
        add(view, "Open in Browser", #selector(openInBrowser), "o", [.command, .shift])
        add(view, "Copy Address", #selector(copyAddress), "c", [.command, .shift])
        view.addItem(.separator())
        add(view, "Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])

        let windowMenu = submenu("Window")
        add(windowMenu, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(windowMenu, "Zoom", #selector(NSWindow.performZoom(_:)))
        windowMenu.addItem(.separator())
        add(windowMenu, "Bring All to Front", #selector(NSApplication.arrangeInFront(_:)))
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
