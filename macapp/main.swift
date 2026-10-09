// 「读书」App 外壳：WKWebView 显示本机 own-reader 服务（默认 http://127.0.0.1:8460/，由 launchd 常驻）。
// 地址与 launchd label 从 Info.plist 的 OwnReaderURL / OwnReaderLaunchdLabel 读，改完重新 build。
// 服务没响应时先 launchctl kickstart 再重试。编译：macapp/build.sh（swiftc 直接编，不需要 Xcode 工程）。

import Cocoa
import WebKit

let baseURL = URL(string: Bundle.main.object(forInfoDictionaryKey: "OwnReaderURL") as? String ?? "http://127.0.0.1:8460/")!
let launchdLabel = Bundle.main.object(forInfoDictionaryKey: "OwnReaderLaunchdLabel") as? String ?? "com.example.ownreader"
let zoomKey = "pageZoom"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        connectAndLoad(attempt: 0)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.preferences.isElementFullscreenEnabled = true
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 1100),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "讀書"
        window.titlebarAppearsTransparent = true
        // 标题栏与纸色页面同色（深色模式跟随系统）
        window.backgroundColor = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0x1c / 255.0, green: 0x1a / 255.0, blue: 0x18 / 255.0, alpha: 1)
                : NSColor(srgbRed: 0xf4 / 255.0, green: 0xef / 255.0, blue: 0xe6 / 255.0, alpha: 1)
        }
        webView.setValue(false, forKey: "drawsBackground")
        window.contentView = webView
        window.setFrameAutosaveName("OwnReaderMain")
        if !window.setFrameUsingName("OwnReaderMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // 服务探活：GET /api/books?q=__ping__，2 秒超时
    func serverReady(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: baseURL.appendingPathComponent("web/style.css"))
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    func connectAndLoad(attempt: Int) {
        serverReady { ok in
            if ok { self.webView.load(URLRequest(url: baseURL)); return }
            if attempt == 0 { self.kickstartServer() }
            if attempt < 20 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.connectAndLoad(attempt: attempt + 1) }
            } else {
                self.webView.loadHTMLString("""
                <body style="font:18px -apple-system;padding:40px">
                <h2>阅读服务没有响应</h2>
                <p>launchd 任务 \(launchdLabel) 拉不起来。检查 launchd plist 里配置的日志文件。</p>
                <p>按 ⌘R 重试。</p></body>
                """, baseURL: nil)
            }
        }
    }

    func kickstartServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/\(launchdLabel)"]
        try? p.run()
    }

    // 外部链接交给默认浏览器
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated,
           url.host != "127.0.0.1", url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // 阅读页不能用整页缩放：WKWebView.pageZoom≠1 时 foliate 分栏的滚动位置会错开半页（2026-10-09 实测 1.4 倍时
    // 左右各露半栏）。阅读页固定 1.0，⌘+/⌘- 改调阅读器字号；书架等其他页面照旧用整页缩放。
    var isReader: Bool { webView.url?.path == "/read" }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        webView.pageZoom = isReader ? 1.0 : (UserDefaults.standard.object(forKey: zoomKey) as? CGFloat ?? 1.0)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let t = webView.title, !t.isEmpty { window.title = t }
    }

    // MARK: 菜单

    @objc func reload() {
        if webView.url == nil || webView.url?.host != "127.0.0.1" { connectAndLoad(attempt: 0) } else { webView.reload() }
    }
    @objc func goBack() { webView.goBack() }
    @objc func goHome() { webView.load(URLRequest(url: baseURL)) }
    @objc func toggleEink() {
        webView.evaluateJavaScript("""
        (() => { const t = document.documentElement.dataset.theme === 'eink' ? 'desk' : 'eink';
          localStorage.setItem('own-reader-theme', t); location.reload(); })()
        """)
    }
    func fontStep(_ d: Int) { webView.evaluateJavaScript("window.ownReaderFontStep && window.ownReaderFontStep(\(d))") }
    @objc func zoomIn() { isReader ? fontStep(6) : setZoom(webView.pageZoom + 0.1) }
    @objc func zoomOut() { isReader ? fontStep(-6) : setZoom(webView.pageZoom - 0.1) }
    @objc func zoomReset() { isReader ? fontStep(0) : setZoom(1.0) }
    func setZoom(_ z: CGFloat) {
        webView.pageZoom = max(0.5, min(3.0, z))
        UserDefaults.standard.set(webView.pageZoom, forKey: zoomKey)
    }

    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem()
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            item.submenu = m
            main.addItem(item)
        }
        func mi(_ t: String, _ a: Selector?, _ k: String, _ target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.target = target
            return i
        }
        add("讀書", [mi("退出讀書", #selector(NSApplication.terminate(_:)), "q")])
        add("编辑", [
            mi("撤销", Selector(("undo:")), "z"),
            mi("剪切", #selector(NSText.cut(_:)), "x"),
            mi("拷贝", #selector(NSText.copy(_:)), "c"),
            mi("粘贴", #selector(NSText.paste(_:)), "v"),
            mi("全选", #selector(NSText.selectAll(_:)), "a"),
        ])
        add("显示", [
            mi("书架", #selector(goHome), "h", self),
            mi("返回", #selector(goBack), "[", self),
            mi("重新载入", #selector(reload), "r", self),
            mi("切换墨水屏模式", #selector(toggleEink), "e", self),
            NSMenuItem.separator(),
            mi("放大", #selector(zoomIn), "+", self),
            mi("缩小", #selector(zoomOut), "-", self),
            mi("实际大小", #selector(zoomReset), "0", self),
        ])
        add("窗口", [mi("最小化", #selector(NSWindow.miniaturize(_:)), "m"),
                    mi("关闭", #selector(NSWindow.performClose(_:)), "w")])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
