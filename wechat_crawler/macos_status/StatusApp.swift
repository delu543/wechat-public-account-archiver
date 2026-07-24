import AppKit
import Foundation

private func statusLog(_ message: String) {
    guard let data = "[status-item] \(message)\n".data(using: .utf8) else { return }
    FileHandle.standardError.write(data)
}

private struct SessionCache: Decodable {
    let status: String
    let message: String
}

private struct Overview: Decodable {
    let subscriptionCount: Int
    let enabledCount: Int
    let pendingExportCount: Int
    let nextRunAt: String?
    let schedulerRunning: Bool
    let wechatSessionCache: SessionCache?
}

private struct CurrentJob: Decodable {
    let accountName: String
    let phase: String
    let progress: Int
    let etaSeconds: Int?
}

private struct LatestWord: Decodable {
    let accountName: String
    let articleCount: Int
    let completedAt: String?
    let wordPath: String?
}

private struct SessionRequest: Decodable {
    let subscriptionId: Int
    let accountName: String
    let accountBiz: String
    let requestKey: String
}

private struct WidgetPayload: Decodable {
    let overview: Overview
    let attentionCount: Int
    let attentionMessage: String
    let currentJob: CurrentJob?
    let latestWord: LatestWord?
    let outputDir: String
    let sessionRequests: [SessionRequest]
}

private final class StatusViewController: NSViewController {
    private let stateLabel = NSTextField(labelWithString: "正在连接本地服务")
    private let subscriptionsValue = NSTextField(labelWithString: "--")
    private let pendingValue = NSTextField(labelWithString: "--")
    private let nextRunValue = NSTextField(labelWithString: "--")
    private let jobLabel = NSTextField(labelWithString: "暂无运行任务")
    private let detailLabel = NSTextField(wrappingLabelWithString: "等待自动计划")
    private let progress = NSProgressIndicator()
    private let progressValue = NSTextField(labelWithString: "0%")
    private let latestWordButton = NSButton(title: "暂无新增 Word", target: nil, action: nil)
    private var outputDir = ""
    private var latestWordPath: String?
    var refreshAction: (() -> Void)?

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 360, height: 330))

        let title = NSTextField(labelWithString: "微信公众号归档")
        title.font = .systemFont(ofSize: 17, weight: .semibold)
        stateLabel.font = .systemFont(ofSize: 12, weight: .medium)
        stateLabel.textColor = .secondaryLabelColor

        let header = NSStackView(views: [title, stateLabel])
        header.orientation = .vertical
        header.alignment = .leading
        header.spacing = 4

        let metrics = NSGridView(views: [
            [metric(title: "订阅", value: subscriptionsValue), metric(title: "待输出", value: pendingValue)],
            [metric(title: "下次运行", value: nextRunValue), metric(title: "微信会话", value: NSTextField(labelWithString: "按需检查"))],
        ])
        metrics.rowSpacing = 10
        metrics.columnSpacing = 20
        metrics.column(at: 0).width = 150
        metrics.column(at: 1).width = 150

        jobLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 3
        progress.minValue = 0
        progress.maxValue = 100
        progress.isIndeterminate = false
        progress.controlSize = .small
        progressValue.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        progressValue.textColor = .secondaryLabelColor

        let progressRow = NSStackView(views: [progress, progressValue])
        progressRow.orientation = .horizontal
        progressRow.spacing = 8
        progress.widthAnchor.constraint(equalToConstant: 286).isActive = true

        latestWordButton.bezelStyle = .inline
        latestWordButton.image = NSImage(systemSymbolName: "doc.text", accessibilityDescription: "Word")
        latestWordButton.imagePosition = .imageLeading
        latestWordButton.alignment = .left
        latestWordButton.target = self
        latestWordButton.action = #selector(openLatestWord)
        latestWordButton.isEnabled = false

        let dashboardButton = NSButton(title: "打开控制台", target: self, action: #selector(openDashboard))
        dashboardButton.image = NSImage(systemSymbolName: "rectangle.grid.2x2", accessibilityDescription: "控制台")
        dashboardButton.imagePosition = .imageLeading
        dashboardButton.bezelStyle = .rounded
        let outputButton = NSButton(title: "打开输出目录", target: self, action: #selector(openOutput))
        outputButton.image = NSImage(systemSymbolName: "folder", accessibilityDescription: "输出目录")
        outputButton.imagePosition = .imageLeading
        outputButton.bezelStyle = .rounded
        let refreshButton = NSButton(title: "", target: self, action: #selector(refreshNow))
        refreshButton.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "刷新")
        refreshButton.bezelStyle = .rounded
        refreshButton.toolTip = "刷新状态"

        let actions = NSStackView(views: [dashboardButton, outputButton, refreshButton])
        actions.orientation = .horizontal
        actions.spacing = 8

        let stack = NSStackView(views: [
            header,
            separator(),
            metrics,
            separator(),
            jobLabel,
            detailLabel,
            progressRow,
            separator(),
            latestWordButton,
            actions,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -14),
        ])
    }

    func update(_ payload: WidgetPayload) {
        outputDir = payload.outputDir
        subscriptionsValue.stringValue = "\(payload.overview.enabledCount)/\(payload.overview.subscriptionCount)"
        pendingValue.stringValue = "\(payload.overview.pendingExportCount) 篇"
        nextRunValue.stringValue = compactDate(payload.overview.nextRunAt)

        if payload.attentionCount > 0 {
            stateLabel.stringValue = "有 \(payload.attentionCount) 个账号需要处理"
            stateLabel.textColor = .systemOrange
        } else if payload.overview.schedulerRunning {
            stateLabel.stringValue = "自动归档运行中"
            stateLabel.textColor = .systemGreen
        } else {
            stateLabel.stringValue = "自动服务未运行"
            stateLabel.textColor = .systemRed
        }

        if let job = payload.currentJob {
            jobLabel.stringValue = "\(job.accountName) · \(phaseName(job.phase))"
            detailLabel.stringValue = job.etaSeconds.map { "预计剩余 \(duration($0))" } ?? "正在处理"
            progress.doubleValue = Double(job.progress)
            progressValue.stringValue = "\(job.progress)%"
        } else {
            jobLabel.stringValue = "暂无运行任务"
            detailLabel.stringValue = payload.attentionMessage.isEmpty ? "等待下一次自动计划" : payload.attentionMessage
            progress.doubleValue = 0
            progressValue.stringValue = "0%"
        }

        latestWordPath = payload.latestWord?.wordPath
        if let word = payload.latestWord {
            latestWordButton.title = "\(word.accountName) · \(word.articleCount) 篇新增"
            latestWordButton.isEnabled = word.wordPath != nil
        } else {
            latestWordButton.title = "暂无新增 Word"
            latestWordButton.isEnabled = false
        }
    }

    func setOffline() {
        stateLabel.stringValue = "本地服务暂不可用"
        stateLabel.textColor = .systemRed
        jobLabel.stringValue = "等待服务恢复"
        detailLabel.stringValue = "自动启动会继续尝试"
    }

    @objc private func openDashboard() {
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:8765")!)
    }

    @objc private func openOutput() {
        guard !outputDir.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: outputDir, isDirectory: true))
    }

    @objc private func openLatestWord() {
        guard let path = latestWordPath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func refreshNow() {
        refreshAction?()
    }

    private func metric(title: String, value: NSTextField) -> NSView {
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .secondaryLabelColor
        value.font = .systemFont(ofSize: 15, weight: .semibold)
        let stack = NSStackView(views: [label, value])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        return stack
    }

    private func separator() -> NSView {
        let box = NSBox()
        box.boxType = .separator
        box.widthAnchor.constraint(equalToConstant: 324).isActive = true
        return box
    }

    private func compactDate(_ value: String?) -> String {
        guard let value, value.count >= 16 else { return "--" }
        let month = value.index(value.startIndex, offsetBy: 5)
        let end = value.index(value.startIndex, offsetBy: 16)
        return String(value[month..<end]).replacingOccurrences(of: "T", with: " ")
    }

    private func duration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds) 秒" }
        return "\(seconds / 60) 分钟"
    }

    private func phaseName(_ phase: String) -> String {
        let names = [
            "preflight": "链接/biz 直连",
            "discover": "发现历史文章",
            "crawl_fast": "抓取正文",
            "retry_failed": "重试失败项",
            "retry_unavailable": "重试不可用项",
            "export_word": "生成 Word",
            "cleanup": "清理缓存",
        ]
        return names[phase] ?? phase
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let controller = StatusViewController()
    private var timer: Timer?
    private var sessionPreparationInFlight = false
    private var serviceOnline: Bool?
    private var lastSessionRequestCount: Int?
    private var lastScreenLocked: Bool?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusLog("status item launched")
        NSApp.setActivationPolicy(.accessory)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "tray.full.fill", accessibilityDescription: "公众号归档")
            button.imagePosition = .imageLeading
            button.title = " 归档"
            button.target = self
            button.action = #selector(togglePopover)
            button.toolTip = "微信公众号自动归档"
        }
        popover.contentViewController = controller
        popover.contentSize = NSSize(width: 360, height: 330)
        popover.behavior = .transient
        controller.refreshAction = { [weak self] in self?.refresh() }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        if !UserDefaults.standard.bool(forKey: "didShowInitialPopover") {
            UserDefaults.standard.set(true, forKey: "didShowInitialPopover")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.showPopover() }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc private func togglePopover() {
        popover.isShown ? popover.performClose(nil) : showPopover()
    }

    private func showPopover() {
        guard let button = statusItem.button else { return }
        refresh()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
    }

    private func refresh() {
        guard let url = URL(string: "http://127.0.0.1:8765/api/automation/status-widget") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, error in
            guard let self else { return }
            guard error == nil, let data else {
                if self.serviceOnline != false {
                    statusLog("status service request failed: \(error?.localizedDescription ?? "no data")")
                }
                self.serviceOnline = false
                DispatchQueue.main.async {
                    self.controller.setOffline()
                    self.statusItem.button?.title = " 归档 !"
                }
                return
            }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            guard let payload = try? decoder.decode(WidgetPayload.self, from: data) else {
                statusLog("status payload decode failed")
                return
            }
            if self.serviceOnline == false { statusLog("status service recovered") }
            self.serviceOnline = true
            if self.lastSessionRequestCount != payload.sessionRequests.count {
                statusLog("session requests: \(payload.sessionRequests.count)")
                self.lastSessionRequestCount = payload.sessionRequests.count
            }
            DispatchQueue.main.async {
                self.controller.update(payload)
                if let job = payload.currentJob {
                    self.statusItem.button?.title = " 归档 \(job.progress)%"
                } else if payload.attentionCount > 0 {
                    self.statusItem.button?.title = " 归档 !"
                } else if payload.overview.pendingExportCount > 0 {
                    self.statusItem.button?.title = " 归档 \(payload.overview.pendingExportCount)"
                } else {
                    self.statusItem.button?.title = " 归档"
                }
                self.prepareNextSessionIfNeeded(
                    payload.sessionRequests,
                    hasCurrentJob: payload.currentJob != nil
                )
            }
        }.resume()
    }

    private func prepareNextSessionIfNeeded(_ requests: [SessionRequest], hasCurrentJob: Bool) {
        guard !sessionPreparationInFlight else { return }
        guard !hasCurrentJob else { return }
        if isScreenLocked() {
            if lastScreenLocked != true { statusLog("screen locked; session preparation deferred") }
            lastScreenLocked = true
            return
        }
        if lastScreenLocked == true { statusLog("screen unlocked; session preparation resumed") }
        lastScreenLocked = false
        let day = ISO8601DateFormatter().string(from: Date()).prefix(10)
        guard let request = requests.first(where: {
            !UserDefaults.standard.bool(forKey: "foreground-session-v3:\(day):\($0.subscriptionId)")
        }) else { return }

        sessionPreparationInFlight = true
        statusLog("starting authorized foreground recovery for \(request.accountName)")
        startForegroundSubscription(request.accountBiz) { [weak self] accepted in
            guard let self else { return }
            if accepted {
                UserDefaults.standard.set(
                    true,
                    forKey: "foreground-session-v3:\(day):\(request.subscriptionId)"
                )
                UserDefaults.standard.synchronize()
                statusLog("account \(request.accountName) foreground recovery finished")
            } else {
                statusLog("account \(request.accountName) foreground recovery failed to start or finish")
            }
            DispatchQueue.main.async {
                self.sessionPreparationInFlight = false
                self.refresh()
            }
        }
    }

    private func isScreenLocked() -> Bool {
        guard let values = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return values["CGSSessionScreenIsLocked"] as? Bool ?? false
    }

    private func startForegroundSubscription(_ accountBiz: String, completion: @escaping (Bool) -> Void) {
        let productRoot = Bundle.main.bundleURL.deletingLastPathComponent()
        let runtime = productRoot.appendingPathComponent("runtime", isDirectory: true)
        let python = runtime.appendingPathComponent(".venv/bin/python")
        let main = runtime.appendingPathComponent("main.py")
        let config = runtime.appendingPathComponent("config.yaml")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: main.path),
              FileManager.default.fileExists(atPath: config.path) else {
            completion(false)
            return
        }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime
        process.arguments = [
            main.path,
            "--config", config.path,
            "automation-run",
            "--account-biz", accountBiz,
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.standardError
        process.terminationHandler = { task in
            statusLog("foreground recovery process exited with status \(task.terminationStatus)")
            // A non-zero CLI status means the public session is still
            // unavailable, not that the foreground helper failed to launch.
            // Record the attempt so the menu item does not retry every poll.
            completion(true)
        }
        do {
            try process.run()
        } catch {
            statusLog("foreground recovery launch error: \(error.localizedDescription)")
            completion(false)
        }
    }
}

@main
private struct StatusAppMain {
    private static let appDelegate = AppDelegate()

    static func main() {
        let application = NSApplication.shared
        application.delegate = appDelegate
        application.run()
    }
}
