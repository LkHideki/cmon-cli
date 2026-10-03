// cmon Bar — Claude quota in the macOS menu bar.
//
// Data comes from `cmon status --json` (local cache, no network; kept fresh by
// `cmon install`). Look and behaviour live in ~/.cmon/bar.json, re-read on every
// refresh, so edits apply without rebuilding. Build/install: menubar/install.sh.

import AppKit

// MARK: - Settings (~/.cmon/bar.json)

struct Settings: Codable {
    /// Menu bar title: non-empty segments joined by `separator`. Placeholders:
    /// {5h} {wk} {reset} {reset_hm} {wk_reset_h} {wk_reset_hm} {age}. The 5h reset
    /// ({reset}, {reset_hm}) is blank unless 5h usage is above `resetAbovePercent`.
    var segments = ["{5h}", "{wk}", "{reset}", "{wk_reset_h}"]
    var separator = "•"
    var resetAbovePercent = 85.0
    var refreshSeconds = 60
    var staleMinutes = 30
    var cmon = "~/.local/bin/cmon"
    var watchCommand = "open -na Ghostty --args -e $HOME/.local/bin/cmon watch"

    enum CodingKeys: String, CodingKey {
        case segments, separator, cmon
        case resetAbovePercent = "reset_above_percent"
        case refreshSeconds = "refresh_seconds"
        case staleMinutes = "stale_minutes"
        case watchCommand = "watch_command"
    }

    static let url = URL(fileURLWithPath: NSString(string: "~/.cmon/bar.json").expandingTildeInPath)

    init() {}

    init(from decoder: Decoder) throws {  // missing keys fall back to defaults
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Settings()
        segments = try c.decodeIfPresent([String].self, forKey: .segments) ?? d.segments
        separator = try c.decodeIfPresent(String.self, forKey: .separator) ?? d.separator
        resetAbovePercent = try c.decodeIfPresent(Double.self, forKey: .resetAbovePercent) ?? d.resetAbovePercent
        refreshSeconds = max(try c.decodeIfPresent(Int.self, forKey: .refreshSeconds) ?? d.refreshSeconds, 10)
        staleMinutes = try c.decodeIfPresent(Int.self, forKey: .staleMinutes) ?? d.staleMinutes
        cmon = try c.decodeIfPresent(String.self, forKey: .cmon) ?? d.cmon
        watchCommand = try c.decodeIfPresent(String.self, forKey: .watchCommand) ?? d.watchCommand
    }

    static func load() -> Settings {
        if let data = try? Data(contentsOf: url), let s = try? JSONDecoder().decode(Settings.self, from: data) {
            return s
        }
        if !FileManager.default.fileExists(atPath: url.path) {  // first run: write editable defaults
            let enc = JSONEncoder()
            enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            try? enc.encode(Settings()).write(to: url)
        }
        return Settings()
    }
}

// MARK: - Data (`cmon status --json`, version 1)

struct Quota: Decodable, Sendable {
    let key: String
    let label: String
    let percent: Double
    let resetsAt: String?

    enum CodingKeys: String, CodingKey {
        case key, label, percent
        case resetsAt = "resets_at"
    }

    var resetDate: Date? { resetsAt.flatMap(parseISO) }
}

struct Snapshot: Decodable, Sendable {
    struct Provider: Decodable, Sendable { let windows: [Quota] }
    let offline: Bool
    let observedAt: String?
    let providers: [String: Provider]

    enum CodingKeys: String, CodingKey {
        case offline, providers
        case observedAt = "observed_at"
    }

    var observedDate: Date? { observedAt.flatMap(parseISO) }
    var windows: [Quota] { providers["claude"]?.windows ?? [] }
    func window(_ key: String) -> Quota? { windows.first { $0.key == key } }
}

/// Python emits microsecond ISO 8601; drop the fraction, second precision is plenty here.
func parseISO(_ s: String) -> Date? {
    let trimmed = s.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
    return ISO8601DateFormatter().date(from: trimmed)
}

// MARK: - Formatting

func compactDuration(_ seconds: TimeInterval) -> String {
    let m = max(Int(seconds / 60), 0)
    if m >= 1440 { return "\(m / 1440)d\(m % 1440 / 60)h" }
    if m >= 60 { return "\(m / 60)h\(m % 60)m" }
    return "\(m)m"
}

func spacedDuration(_ seconds: TimeInterval) -> String {
    let m = max(Int(seconds / 60), 0)
    if m >= 1440 { return "\(m / 1440)d \(m % 1440 / 60)h" }
    if m >= 60 { return "\(m / 60)h \(m % 60)m" }
    return "\(m) min"
}

func renderTitle(_ settings: Settings, _ snap: Snapshot, now: Date) -> String {
    let session = snap.window("session"), weekly = snap.window("weekly_all")
    func pct(_ q: Quota?) -> String { q.map { String(Int($0.percent.rounded())) } ?? "–" }
    func left(_ q: Quota?) -> TimeInterval? {
        guard let d = q?.resetDate, d > now else { return nil }
        return d.timeIntervalSince(now)
    }
    let hot = (session?.percent ?? 0) > settings.resetAbovePercent
    let values: [String: String] = [
        "5h": pct(session),
        "wk": pct(weekly),
        "reset": hot ? left(session).map { String(Int($0 / 60)) } ?? "" : "",
        "reset_hm": hot ? left(session).map(compactDuration) ?? "" : "",
        "wk_reset_h": left(weekly).map { String(Int($0 / 3600)) } ?? "–",
        "wk_reset_hm": left(weekly).map(compactDuration) ?? "–",
        "age": snap.observedDate.map { String(Int(now.timeIntervalSince($0) / 60)) } ?? "–",
    ]
    return settings.segments
        .map { seg in values.reduce(seg) { $0.replacingOccurrences(of: "{\($1.key)}", with: $1.value) } }
        .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        .joined(separator: settings.separator)
}

func displayName(_ q: Quota) -> String {
    if q.key == "session" { return "Session · 5h" }
    if q.key.hasPrefix("weekly") { return "Weekly · \(q.label)" }
    return q.label
}

func resetDetail(_ q: Quota, now: Date) -> String {
    guard let d = q.resetDate else { return "No reset scheduled" }
    if d <= now { return "Reset pending" }
    let f = DateFormatter()
    f.dateFormat = d.timeIntervalSince(now) < 86_400 ? "HH:mm" : "EEE HH:mm"
    return "Resets in \(spacedDuration(d.timeIntervalSince(now))) · \(f.string(from: d))"
}

// MARK: - Menu row: label, percent, thin progress bar, reset line

final class QuotaRowView: NSView {
    private let title: String, percent: Double, detail: String

    init(title: String, percent: Double, detail: String) {
        self.title = title
        self.percent = percent
        self.detail = detail
        super.init(frame: NSRect(x: 0, y: 0, width: 280, height: 56))
        autoresizingMask = .width
    }

    required init?(coder: NSCoder) { fatalError("unused") }

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let inset: CGFloat = 14, width = bounds.width - inset * 2
        let body: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13),
                                                   .foregroundColor: NSColor.labelColor]
        let figure: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .semibold),
            .foregroundColor: NSColor.labelColor]
        let caption: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 11),
                                                      .foregroundColor: NSColor.secondaryLabelColor]

        (title as NSString).draw(at: NSPoint(x: inset, y: 6), withAttributes: body)
        let value = "\(Int(percent.rounded()))%" as NSString
        value.draw(at: NSPoint(x: inset + width - value.size(withAttributes: figure).width, y: 6),
                   withAttributes: figure)

        let track = NSRect(x: inset, y: 28, width: width, height: 4)
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: track, xRadius: 2, yRadius: 2).fill()
        let fraction = min(max(percent, 0), 100) / 100
        if fraction > 0 {
            tint.setFill()
            var fill = track
            fill.size.width = max(width * fraction, 4)
            NSBezierPath(roundedRect: fill, xRadius: 2, yRadius: 2).fill()
        }

        (detail as NSString).draw(at: NSPoint(x: inset, y: 37), withAttributes: caption)
    }

    private var tint: NSColor {
        percent >= 95 ? .systemRed : percent >= 80 ? .systemOrange : .controlAccentColor
    }
}

// MARK: - App

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var settings = Settings.load()
    private var snapshot: Snapshot?
    private var error: String?
    private var timer: Timer?
    private var busy = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        item.button?.title = "cmon"
        item.button?.appearsDisabled = true
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.refresh() } }
        refresh()
    }

    private func schedule() {
        let interval = TimeInterval(settings.refreshSeconds)
        guard timer?.timeInterval != interval else { return }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }

    private func refresh(live: Bool = false) {
        settings = Settings.load()
        schedule()
        guard !busy else { return }
        busy = true
        let path = NSString(string: settings.cmon).expandingTildeInPath
        let args = live ? ["status", "--json", "--live"] : ["status", "--json"]
        Task.detached {
            let result = Self.fetch(path, args)
            await MainActor.run {
                self.busy = false
                switch result {
                case .success(let snap): self.snapshot = snap; self.error = nil
                case .failure(let err): self.error = err.message
                }
                self.render()
            }
        }
    }

    struct FetchError: Error { let message: String }

    nonisolated private static func fetch(_ path: String, _ args: [String]) -> Result<Snapshot, FetchError> {
        guard FileManager.default.isExecutableFile(atPath: path) else {
            return .failure(FetchError(message: "cmon not found at \(path)"))
        }
        let proc = Process(), out = Pipe()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = args
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return .failure(FetchError(message: "cmon failed to start")) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let snap = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            return .failure(FetchError(message: "cmon status --json unavailable (update cmon)"))
        }
        return .success(snap)
    }

    private func isStale(_ snap: Snapshot, now: Date) -> Bool {
        guard let observed = snap.observedDate else { return true }
        return now.timeIntervalSince(observed) > TimeInterval(settings.staleMinutes * 60)
    }

    private func render() {
        guard let button = item.button else { return }
        let now = Date()
        guard let snap = snapshot, !snap.offline else {
            button.title = "–"
            button.appearsDisabled = true
            button.toolTip = error ?? "cmon offline"
            return
        }
        button.title = renderTitle(settings, snap, now: now)
        button.appearsDisabled = isStale(snap, now: now) || error != nil
        button.toolTip = snap.windows.map { "\(displayName($0)) \(Int($0.percent.rounded()))%" }
            .joined(separator: "  ·  ")
    }

    // Rebuilt on open so relative times are always current.
    func menuNeedsUpdate(_ menu: NSMenu) {
        let now = Date()
        menu.removeAllItems()
        menu.addItem(.sectionHeader(title: "Claude"))
        if let snap = snapshot, !snap.offline {
            for q in snap.windows {
                let row = NSMenuItem()
                row.view = QuotaRowView(title: displayName(q), percent: q.percent, detail: resetDetail(q, now: now))
                menu.addItem(row)
            }
            menu.addItem(.separator())
            let age = snap.observedDate.map { spacedDuration(now.timeIntervalSince($0)) } ?? "–"
            let updated = NSMenuItem(title: "Updated \(age) ago", action: nil, keyEquivalent: "")
            updated.isEnabled = false
            menu.addItem(updated)
            if isStale(snap, now: now) {
                let hint = NSMenuItem(title: "Data is stale — schedule collection with `cmon install`",
                                      action: nil, keyEquivalent: "")
                hint.isEnabled = false
                menu.addItem(hint)
            }
        } else {
            let msg = NSMenuItem(title: "Offline — no usage data", action: nil, keyEquivalent: "")
            msg.isEnabled = false
            menu.addItem(msg)
        }
        if let error {
            let msg = NSMenuItem(title: error, action: nil, keyEquivalent: "")
            msg.isEnabled = false
            menu.addItem(msg)
        }
        menu.addItem(.separator())
        menu.addItem(action("Refresh Now", #selector(refreshNow), key: "r"))
        menu.addItem(action("Open cmon watch", #selector(openWatch), key: ""))
        menu.addItem(action("Settings…", #selector(openSettings), key: ","))
        menu.addItem(.separator())
        menu.addItem(action("Quit cmon Bar", #selector(quit), key: "q"))
    }

    private func action(_ title: String, _ selector: Selector, key: String) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        it.target = self
        return it
    }

    @objc private func refreshNow() { refresh(live: true) }

    @objc private func openWatch() { shell(settings.watchCommand) }

    @objc private func openSettings() { shell("open -t \"\(Settings.url.path)\"") }

    @objc private func quit() { NSApp.terminate(nil) }

    private func shell(_ command: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/zsh")
        proc.arguments = ["-lc", command]
        try? proc.run()
    }
}

@main
enum CmonBar {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
