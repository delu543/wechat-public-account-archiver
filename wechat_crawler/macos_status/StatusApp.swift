import AppKit
import Foundation
import UserNotifications

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
    let automationPaused: Bool
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

private struct CompletionEvent: Decodable {
    let cycleKey: String
    let scheduledFor: String
    let completedAt: String?
    let outputFolder: String
    let accountCount: Int
    let exportedCount: Int
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
    let latestCompletion: CompletionEvent?
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
    private let pauseAllButton = NSButton(title: "暂停全部爬取", target: nil, action: nil)
    private let latestWordButton = NSButton(title: "暂无新增 Word", target: nil, action: nil)
    private var outputDir = ""
    private var latestWordPath: String?
    private var automationPaused = false
    var refreshAction: (() -> Void)?
    var pauseAction: ((Bool) -> Void)?

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 360, height: 378))

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

        pauseAllButton.bezelStyle = .rounded
        pauseAllButton.image = NSImage(systemSymbolName: "pause.circle", accessibilityDescription: "暂停全部爬取")
        pauseAllButton.imagePosition = .imageLeading
        pauseAllButton.target = self
        pauseAllButton.action = #selector(togglePauseAll)
        pauseAllButton.widthAnchor.constraint(equalToConstant: 324).isActive = true

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
            pauseAllButton,
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
        automationPaused = payload.overview.automationPaused
        pauseAllButton.title = automationPaused ? "继续全部爬取" : "暂停全部爬取"
        pauseAllButton.image = NSImage(
            systemSymbolName: automationPaused ? "play.circle.fill" : "pause.circle",
            accessibilityDescription: pauseAllButton.title
        )
        pauseAllButton.isEnabled = true

        if automationPaused {
            stateLabel.stringValue = "全部爬取已暂停"
            stateLabel.textColor = .systemOrange
        } else if payload.attentionCount > 0 {
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
            if !payload.attentionMessage.isEmpty {
                detailLabel.stringValue = payload.attentionMessage
            } else if let completion = payload.latestCompletion {
                detailLabel.stringValue = "上轮已经弄好了 · \(completion.accountCount) 个公众号 · 新增 \(completion.exportedCount) 篇"
            } else {
                detailLabel.stringValue = "等待下一次自动计划"
            }
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
        pauseAllButton.isEnabled = false
    }

    func setPauseUpdateFailed() {
        stateLabel.stringValue = "暂停状态更新失败"
        stateLabel.textColor = .systemRed
        pauseAllButton.isEnabled = true
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

    @objc private func togglePauseAll() {
        pauseAllButton.isEnabled = false
        pauseAction?(!automationPaused)
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

private final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let popover = NSPopover()
    private let controller = StatusViewController()
    private var timer: Timer?
    private var currentStatusBadge = ""
    private var sessionPreparationInFlight = false
    private var foregroundSessionProcess: Process?
    private var automationPaused = false
    private var serviceOnline: Bool?
    private var lastSessionRequestCount: Int?
    private var lastScreenLocked: Bool?
    private let notificationCenter = UNUserNotificationCenter.current()
    private let maxSessionPreparationAttempts = 3
    private let sessionPreparationCooldown: TimeInterval = 60

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusLog("status item launched")
        NSApp.setActivationPolicy(.accessory)
        notificationCenter.delegate = self
        notificationCenter.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error {
                statusLog("notification authorization failed: \(error.localizedDescription)")
            } else {
                statusLog("notification authorization: \(granted ? "granted" : "not granted")")
            }
        }
        configureStatusButton()
        statusLog("status item visible: \(statusItem.isVisible)")
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(workspaceDidWake),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
        popover.contentViewController = controller
        popover.contentSize = NSSize(width: 360, height: 378)
        popover.behavior = .transient
        controller.refreshAction = { [weak self] in self?.refresh() }
        controller.pauseAction = { [weak self] paused in self?.setAutomationPaused(paused) }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.ensureStatusItemVisible()
            self?.refresh()
        }
        let visibilityOnboardingKey = "didShowVisibilityPopover-v2"
        if !UserDefaults.standard.bool(forKey: visibilityOnboardingKey) {
            UserDefaults.standard.set(true, forKey: visibilityOnboardingKey)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.showPopover() }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc private func togglePopover() {
        popover.isShown ? popover.performClose(nil) : showPopover()
    }

    @objc private func workspaceDidWake() {
        ensureStatusItemVisible()
        refresh()
    }

    private func configureStatusButton() {
        statusItem.isVisible = true
        guard let button = statusItem.button else { return }
        button.image = NSImage(
            systemSymbolName: "tray.full.fill",
            accessibilityDescription: "公众号归档"
        )
        button.image?.isTemplate = true
        button.target = self
        button.action = #selector(togglePopover)
        button.toolTip = "微信公众号自动归档"
        setStatusBadge(currentStatusBadge)
    }

    private func ensureStatusItemVisible() {
        if !statusItem.isVisible {
            statusLog("status item visibility restored")
        }
        statusItem.isVisible = true
        if statusItem.button?.target == nil {
            configureStatusButton()
        }
    }

    private func setStatusBadge(_ value: String) {
        currentStatusBadge = value
        statusItem.isVisible = true
        statusItem.length = value.isEmpty ? NSStatusItem.squareLength : NSStatusItem.variableLength
        guard let button = statusItem.button else { return }
        button.title = value.isEmpty ? "" : " \(value)"
        button.imagePosition = value.isEmpty ? .imageOnly : .imageLeading
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
                    self.setStatusBadge("!")
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
                self.automationPaused = payload.overview.automationPaused
                self.controller.update(payload)
                if payload.overview.automationPaused {
                    self.setStatusBadge("暂停")
                } else if let job = payload.currentJob {
                    self.setStatusBadge("\(job.progress)%")
                } else if payload.attentionCount > 0 {
                    self.setStatusBadge("!")
                } else if payload.overview.pendingExportCount > 0 {
                    self.setStatusBadge("\(payload.overview.pendingExportCount)")
                } else {
                    self.setStatusBadge("")
                }
                self.handleCompletion(payload.latestCompletion)
                if payload.overview.automationPaused {
                    self.cancelForegroundSessionPreparation()
                } else {
                    self.prepareNextSessionIfNeeded(
                        payload.sessionRequests,
                        hasCurrentJob: payload.currentJob != nil
                    )
                }
            }
        }.resume()
    }

    private func setAutomationPaused(_ paused: Bool) {
        automationPaused = paused
        if paused {
            cancelForegroundSessionPreparation()
        }
        guard let url = URL(string: "http://127.0.0.1:8765/api/automation/control") else {
            controller.setPauseUpdateFailed()
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["paused": paused])
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            guard let self else { return }
            guard error == nil,
                  let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode)
            else {
                DispatchQueue.main.async {
                    self.controller.setPauseUpdateFailed()
                    self.refresh()
                }
                return
            }
            DispatchQueue.main.async { self.refresh() }
        }.resume()
    }

    private func cancelForegroundSessionPreparation() {
        guard sessionPreparationInFlight || foregroundSessionProcess != nil else { return }
        statusLog("automation paused; cancelling foreground recovery")
        if let process = foregroundSessionProcess, process.isRunning {
            process.terminate()
        }
        foregroundSessionProcess = nil
        sessionPreparationInFlight = false
    }

    private func handleCompletion(_ completion: CompletionEvent?) {
        guard let completion else { return }
        let defaultsKey = "last-weekly-completion-v1"
        guard UserDefaults.standard.string(forKey: defaultsKey) != completion.cycleKey else { return }
        notificationCenter.getNotificationSettings { [weak self] settings in
            guard let self else { return }
            switch settings.authorizationStatus {
            case .authorized, .provisional:
                self.deliverCompletionNotification(completion, defaultsKey: defaultsKey)
            case .notDetermined:
                self.notificationCenter.requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    if granted {
                        self.deliverCompletionNotification(completion, defaultsKey: defaultsKey)
                    } else {
                        self.showCompletionFallback(completion, defaultsKey: defaultsKey)
                    }
                }
            case .denied:
                self.showCompletionFallback(completion, defaultsKey: defaultsKey)
            @unknown default:
                self.showCompletionFallback(completion, defaultsKey: defaultsKey)
            }
        }
    }

    private func deliverCompletionNotification(_ completion: CompletionEvent, defaultsKey: String) {
        let content = UNMutableNotificationContent()
        content.title = "微信公众号每周归档"
        content.body = "已经弄好了。本周共处理 \(completion.accountCount) 个公众号，新增 \(completion.exportedCount) 篇。点击打开文件夹。"
        content.sound = .default
        content.userInfo = [
            "cycle_key": completion.cycleKey,
            "output_folder": completion.outputFolder,
        ]
        let request = UNNotificationRequest(
            identifier: "weekly-completion-\(completion.cycleKey)",
            content: content,
            trigger: nil
        )
        notificationCenter.add(request) { [weak self] error in
            if let error {
                statusLog("completion notification failed: \(error.localizedDescription)")
                self?.showCompletionFallback(completion, defaultsKey: defaultsKey)
                return
            }
            self?.markCompletionHandled(completion.cycleKey, defaultsKey: defaultsKey)
        }
    }

    private func showCompletionFallback(_ completion: CompletionEvent, defaultsKey: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            guard UserDefaults.standard.string(forKey: defaultsKey) != completion.cycleKey else { return }
            let alert = NSAlert()
            alert.messageText = "已经弄好了"
            alert.informativeText = "本周共处理 \(completion.accountCount) 个公众号，新增 \(completion.exportedCount) 篇。"
            alert.addButton(withTitle: "打开文件夹")
            alert.addButton(withTitle: "稍后")
            NSApp.activate(ignoringOtherApps: true)
            let response = alert.runModal()
            self.markCompletionHandled(completion.cycleKey, defaultsKey: defaultsKey)
            if response == .alertFirstButtonReturn {
                NSWorkspace.shared.open(
                    URL(fileURLWithPath: completion.outputFolder, isDirectory: true)
                )
            }
        }
    }

    private func markCompletionHandled(_ cycleKey: String, defaultsKey: String) {
        UserDefaults.standard.set(cycleKey, forKey: defaultsKey)
        UserDefaults.standard.synchronize()
        statusLog("weekly completion handled: \(cycleKey)")
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier,
              let path = response.notification.request.content.userInfo["output_folder"] as? String
        else { return }
        DispatchQueue.main.async {
            NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true))
        }
    }

    private func prepareNextSessionIfNeeded(_ requests: [SessionRequest], hasCurrentJob: Bool) {
        guard !automationPaused else { return }
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
            sessionPreparationAllowed(day: day, subscriptionId: $0.subscriptionId)
        }) else { return }

        sessionPreparationInFlight = true
        let attempt = recordSessionPreparationAttempt(
            day: day,
            subscriptionId: request.subscriptionId
        )
        statusLog(
            "starting authorized foreground recovery for \(request.accountName) " +
            "(attempt \(attempt)/\(maxSessionPreparationAttempts))"
        )
        startForegroundSessionPreparation(request.accountBiz) { [weak self] prepared in
            guard let self else { return }
            guard prepared else {
                statusLog(
                    "account \(request.accountName) foreground preparation did not complete " +
                    "(attempt \(attempt)/\(self.maxSessionPreparationAttempts))"
                )
                DispatchQueue.main.async {
                    self.sessionPreparationInFlight = false
                    self.refresh()
                }
                return
            }
            statusLog("account \(request.accountName) public seed opened; requesting background resume")
            self.resumeSubscriptionAfterSession(request.subscriptionId) { accepted in
                statusLog(
                    accepted
                        ? "account \(request.accountName) background resume accepted"
                        : "account \(request.accountName) background resume request failed"
                )
                DispatchQueue.main.async {
                    self.sessionPreparationInFlight = false
                    self.refresh()
                }
            }
        }
    }

    private func sessionPreparationAllowed(day: Substring, subscriptionId: Int) -> Bool {
        let defaults = UserDefaults.standard
        let attemptsKey = "foreground-session-v6-attempts:\(day):\(subscriptionId)"
        let attempts = defaults.integer(forKey: attemptsKey)
        if attempts >= maxSessionPreparationAttempts { return false }
        let lastKey = "foreground-session-v6-last:\(day):\(subscriptionId)"
        let lastAttempt = defaults.double(forKey: lastKey)
        return lastAttempt <= 0 || Date().timeIntervalSince1970 - lastAttempt >= sessionPreparationCooldown
    }

    private func recordSessionPreparationAttempt(day: Substring, subscriptionId: Int) -> Int {
        let defaults = UserDefaults.standard
        let attemptsKey = "foreground-session-v6-attempts:\(day):\(subscriptionId)"
        let nextAttempt = defaults.integer(forKey: attemptsKey) + 1
        defaults.set(nextAttempt, forKey: attemptsKey)
        defaults.set(
            Date().timeIntervalSince1970,
            forKey: "foreground-session-v6-last:\(day):\(subscriptionId)"
        )
        defaults.synchronize()
        return nextAttempt
    }

    private func isScreenLocked() -> Bool {
        guard let values = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return values["CGSSessionScreenIsLocked"] as? Bool ?? false
    }

    private func startForegroundSessionPreparation(
        _ accountBiz: String,
        completion: @escaping (Bool) -> Void
    ) {
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
            "automation-prepare-session",
            "--account-biz", accountBiz,
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.standardError
        process.terminationHandler = { task in
            statusLog("foreground recovery process exited with status \(task.terminationStatus)")
            DispatchQueue.main.async { [weak self, weak process] in
                guard let self, let process else { return }
                if self.foregroundSessionProcess === process {
                    self.foregroundSessionProcess = nil
                }
            }
            completion(task.terminationStatus == 0)
        }
        foregroundSessionProcess = process
        do {
            try process.run()
        } catch {
            foregroundSessionProcess = nil
            statusLog("foreground recovery launch error: \(error.localizedDescription)")
            completion(false)
        }
    }

    private func resumeSubscriptionAfterSession(
        _ subscriptionId: Int,
        completion: @escaping (Bool) -> Void
    ) {
        guard let url = URL(
            string: "http://127.0.0.1:8765/api/automation/subscriptions/" +
                "\(subscriptionId)/resume-after-session"
        ) else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        URLSession.shared.dataTask(with: request) { _, response, error in
            guard error == nil,
                  let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode)
            else {
                completion(false)
                return
            }
            completion(true)
        }.resume()
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
