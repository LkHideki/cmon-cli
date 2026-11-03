// cmon Bar — Claude quota in the macOS menu bar.
//
// Data comes from `cmon status --json --live` on the schedule set by the `refresh` rules
// (each call also warms the cache the statusline reads). Look and behaviour live in
// ~/.cmon/bar.json, re-read on every refresh, so edits apply without rebuilding.
// Build/install: menubar/install.sh.

import AppKit

// MARK: - Settings (~/.cmon/bar.json)

/// One refresh rule. It applies when all of its conditions hold (none = always); among the
/// rules that apply, the shortest `every_s` wins. Conditions look at the 5h window:
/// `reset_within_min` — its reset is at most this many minutes away;
/// `usage_above_pct` — its usage is above this percent.
struct RefreshRule: Codable, Equatable {
    var resetWithinMin: Double?
    var usageAbovePct: Double?
    var everyS: Double

    static let minimumEvery: Double = 30  // the usage endpoint is shared with claude.ai: be polite

    enum CodingKeys: String, CodingKey {
        case resetWithinMin = "reset_within_min"
        case usageAbovePct = "usage_above_pct"
        case everyS = "every_s"
    }

    func applies(resetLeft: TimeInterval?, usage: Double?) -> Bool {
        if let m = resetWithinMin {
            guard let left = resetLeft, left > 0, left <= m * 60 else { return false }
        }
        if let p = usageAbovePct {
            guard let u = usage, u > p else { return false }
        }
        return true
    }
}

struct Settings: Codable {
    /// Menu bar title: segments joined by `separator`; an empty segment keeps its slot
    /// (`2%•44%••91h`), so positions never shift. Placeholders: {5h_pct} {wk_pct}
    /// {reset_short} {wk_reset_short} carry their unit; {5h} {wk} {reset} {reset_hm}
    /// {wk_reset_h} {wk_reset_hm} {age} are bare numbers. The 5h reset ({reset_short},
    /// {reset}, {reset_hm}) is blank unless 5h usage is above `resetAbovePercent` or the
    /// reset is at most `resetWithinMin` minutes away.
    var segments = Settings.defaultSegments
    var separator = "•"
    var resetAbovePercent = 85.0
    var resetWithinMin = 100.0
    /// How often to query the API, tightening as the 5h reset or limit approaches.
    var refresh: [RefreshRule] = [
        RefreshRule(everyS: 600),
        RefreshRule(resetWithinMin: 120, everyS: 180),
        RefreshRule(resetWithinMin: 90, everyS: 120),
        RefreshRule(resetWithinMin: 60, everyS: 59),
        RefreshRule(usageAbovePct: 87, everyS: 59),
    ]
    var staleMinutes = 30
    var cmon = "~/.local/bin/cmon"
    var watchCommand = #"osascript -e 'tell application "Terminal" to do script "~/.local/bin/cmon watch"' -e 'tell application "Terminal" to activate'"#

    enum CodingKeys: String, CodingKey {
        case segments, separator, refresh, cmon
        case resetAbovePercent = "reset_above_percent"
        case resetWithinMin = "reset_within_min"
        case staleMinutes = "stale_minutes"
        case watchCommand = "watch_command"
    }

    static let defaultSegments = ["{5h_pct}", "{wk_pct}", "{reset_short}", "{wk_reset_short}"]
    static let url = URL(fileURLWithPath: NSString(string: "~/.cmon/bar.json").expandingTildeInPath)

    init() {}

    init(from decoder: Decoder) throws {  // missing keys fall back to defaults
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Settings()
        segments = try c.decodeIfPresent([String].self, forKey: .segments) ?? d.segments
        separator = try c.decodeIfPresent(String.self, forKey: .separator) ?? d.separator
        resetAbovePercent = try c.decodeIfPresent(Double.self, forKey: .resetAbovePercent) ?? d.resetAbovePercent
        resetWithinMin = try c.decodeIfPresent(Double.self, forKey: .resetWithinMin) ?? d.resetWithinMin
        let rules = (try c.decodeIfPresent([RefreshRule].self, forKey: .refresh) ?? d.refresh)
            .filter { $0.everyS.isFinite && $0.everyS > 0 }
        refresh = rules.isEmpty ? d.refresh : rules
        staleMinutes = try c.decodeIfPresent(Int.self, forKey: .staleMinutes) ?? d.staleMinutes
        cmon = try c.decodeIfPresent(String.self, forKey: .cmon) ?? d.cmon
        watchCommand = try c.decodeIfPresent(String.self, forKey: .watchCommand) ?? d.watchCommand
    }

    /// Seconds between API queries right now; boundary = when a tighter rule kicks in.
    func refreshInterval(resetLeft: TimeInterval?, usage: Double?) -> TimeInterval {
        let every = refresh.filter { $0.applies(resetLeft: resetLeft, usage: usage) }.map(\.everyS).min()
        return max(every ?? 600, RefreshRule.minimumEvery)
    }

    /// Seconds until a `reset_within_min` rule not yet active starts applying, if any.
    func nextBoundary(resetLeft: TimeInterval?) -> TimeInterval? {
        guard let left = resetLeft, left > 0 else { return nil }
        return refresh.compactMap { $0.resetWithinMin }.map { left - $0 * 60 }.filter { $0 > 0 }.min()
    }

    /// Settings plus a problem to show in the menu: a broken bar.json must not fail silently.
    static func load() -> (Settings, String?) {
        if let data = try? Data(contentsOf: url) {
            do {
                return (try JSONDecoder().decode(Settings.self, from: data), nil)
            } catch {
                return (Settings(), "bar.json is invalid — using defaults")
            }
        }
        let enc = JSONEncoder()  // first run: write editable defaults
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        try? enc.encode(Settings()).write(to: url)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        return (Settings(), nil)
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

// Under a minute, durations count in seconds: "0 min" reads as frozen.
func compactDuration(_ seconds: TimeInterval) -> String {
    if seconds < 60 { return "\(max(Int(seconds), 0))s" }
    let m = Int(seconds / 60)
    if m >= 1440 { return "\(m / 1440)d\(m % 1440 / 60)h" }
    if m >= 60 { return "\(m / 60)h\(m % 60)m" }
    return "\(m)m"
}

func spacedDuration(_ seconds: TimeInterval) -> String {
    if seconds < 60 { return "\(max(Int(seconds), 0))s" }
    let m = Int(seconds / 60)
    if m >= 1440 { return "\(m / 1440)d \(m % 1440 / 60)h" }
    if m >= 60 { return "\(m / 60)h \(m % 60)m" }
    return "\(m) min"
}

/// Whether the title shows the 5h countdown ({reset}/{reset_hm}): usage above
/// reset_above_percent, or the reset at most reset_within_min minutes away.
func showsSessionReset(_ settings: Settings, _ snap: Snapshot, now: Date) -> Bool {
    guard let session = snap.window("session") else { return false }
    if session.percent > settings.resetAbovePercent { return true }
    guard let left = session.resetDate?.timeIntervalSince(now), left > 0 else { return false }
    return left <= settings.resetWithinMin * 60
}

/// Duration in one unit, for the title: 91h, 75m, 42s. Hours never round down to "0h".
func unitDuration(_ seconds: TimeInterval, hours: Bool) -> String {
    if seconds < 60 { return "\(Int(seconds))s" }
    if hours && seconds >= 3600 { return "\(Int(seconds / 3600))h" }
    return "\(Int(seconds / 60))m"
}

/// What the title's positions mean, shown in the menu and tooltip; nil for a custom layout.
func titleLegend(_ settings: Settings) -> String? {
    settings.segments == Settings.defaultSegments ? "5h • week • 5h reset • week reset" : nil
}

func renderTitle(_ settings: Settings, _ snap: Snapshot, now: Date) -> String {
    let session = snap.window("session"), weekly = snap.window("weekly_all")
    func pct(_ q: Quota?) -> String { q.map { String(Int($0.percent.rounded())) } ?? "–" }
    func pctUnit(_ q: Quota?) -> String { q.map { "\(Int($0.percent.rounded()))%" } ?? "–" }
    func left(_ q: Quota?) -> TimeInterval? {
        guard let d = q?.resetDate, d > now else { return nil }
        return d.timeIntervalSince(now)
    }
    let hot = showsSessionReset(settings, snap, now: now)
    let values: [String: String] = [
        "5h": pct(session),
        "wk": pct(weekly),
        "5h_pct": pctUnit(session),
        "wk_pct": pctUnit(weekly),
        "reset_short": hot ? left(session).map { unitDuration($0, hours: false) } ?? "" : "",
        "wk_reset_short": left(weekly).map { unitDuration($0, hours: true) } ?? "–",
        "reset": hot ? left(session).map { $0 < 60 ? "\(Int($0))s" : String(Int($0 / 60)) } ?? "" : "",
        "reset_hm": hot ? left(session).map(compactDuration) ?? "" : "",
        "wk_reset_h": left(weekly).map { String(Int($0 / 3600)) } ?? "–",
        "wk_reset_hm": left(weekly).map(compactDuration) ?? "–",
        "age": snap.observedDate.map { String(Int(now.timeIntervalSince($0) / 60)) } ?? "–",
    ]
    return settings.segments
        .map { seg in values.reduce(seg) { $0.replacingOccurrences(of: "{\($1.key)}", with: $1.value) } }
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
    private let title: String, percent: Double
    var detail: String { didSet { if detail != oldValue { needsDisplay = true } } }

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
    private var settings = Settings()
    private var settingsProblem: String?
    private var snapshot: Snapshot?
    private var error: String?
    private var timer: Timer?          // one-shot: the next scheduled API query
    private var lastAttempt: Date?     // last query, successful or not (failures wait a full interval)
    private var busy = false
    // 1 Hz ticker: only redraws local text (no cmon, no network), and only runs while the menu
    // is open or the 5h reset is under a minute away — so countdowns move second by second
    // exactly when someone is looking, and the app is otherwise idle.
    private var ticker: Timer?
    private var menuOpen = false
    private var rowViews: [String: QuotaRowView] = [:]
    private var updatedItem: NSMenuItem?
    private var lastResetPoll: Date?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One bar only: launchd starts it at login, and `open` (or Spotlight) can start another.
        let me = NSRunningApplication.current
        if NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .contains(where: { $0 != me }) {
            exit(0)  // clean exit: launchd's KeepAlive (crash-only) won't respawn it
        }
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        item.button?.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        item.button?.title = "cmon"
        item.button?.appearsDisabled = true
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.refresh(live: true) } }
        reloadSettings()
        refresh(live: false)  // instant first frame from the cache; schedule() goes live as due
    }

    private func reloadSettings() {
        (settings, settingsProblem) = Settings.load()
    }

    private var currentInterval: TimeInterval {
        let session = snapshot?.offline == false ? snapshot?.window("session") : nil
        return settings.refreshInterval(resetLeft: sessionLeft, usage: session?.percent)
    }

    /// When the next API query is due: one interval after the newer of the data's own age and
    /// the last attempt, so cached data that is already fresh isn't refetched and a failure
    /// isn't retried in a loop.
    private var nextDue: Date {
        let base = [snapshot?.observedDate, lastAttempt].compactMap { $0 }.max() ?? .distantPast
        return base.addingTimeInterval(currentInterval)
    }

    private var sessionLeft: TimeInterval? { snapshot?.window("session")?.resetDate?.timeIntervalSinceNow }

    private func updateTicker() {
        let near = sessionLeft.map { $0 > -120 && $0 <= 60 } ?? false  // last minute + catch-up
        // A visible countdown in the title must move on its own, not only when data refreshes.
        let counting = snapshot.map { !$0.offline && showsSessionReset(settings, $0, now: Date()) } ?? false
        if menuOpen || near || counting {
            guard ticker == nil else { return }
            let t = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.tick() }
            }
            t.tolerance = 0.1
            RunLoop.main.add(t, forMode: .common)
            ticker = t
        } else {
            ticker?.invalidate()
            ticker = nil
        }
    }

    private func tick() {
        let now = Date()
        render()
        if menuOpen, let snap = snapshot {
            for q in snap.windows { rowViews[q.key]?.detail = resetDetail(q, now: now) }
            updatedItem?.title = updatedTitle(snap, now: now)
        }
        // The window just rolled over: fetch the new cycle (every 5 s until it shows up).
        if let left = sessionLeft, left <= 0, now.timeIntervalSince(lastResetPoll ?? .distantPast) >= 5 {
            lastResetPoll = now
            refresh(live: true)
        }
        updateTicker()
    }

    private func updatedTitle(_ snap: Snapshot, now: Date) -> String {
        guard let observed = snap.observedDate else { return "Updated –" }
        let age = now.timeIntervalSince(observed)
        let updated = age < 5 ? "Updated just now" : "Updated \(spacedDuration(age)) ago"
        if busy { return updated + " · updating…" }
        let next = nextDue.timeIntervalSince(now)
        return next > 0 ? updated + " · next in \(spacedDuration(next))" : updated
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuOpen = true
        updateTicker()
    }

    func menuDidClose(_ menu: NSMenu) {
        menuOpen = false
        updateTicker()
    }

    /// Arm a one-shot timer for the next due query — or earlier, at the moment a tighter
    /// `reset_within_min` rule starts applying, so a 10-minute wait can't overshoot it.
    private func schedule() {
        timer?.invalidate()
        let now = Date()
        var fire = max(nextDue, now.addingTimeInterval(1))
        if let boundary = settings.nextBoundary(resetLeft: sessionLeft) {
            fire = min(fire, now.addingTimeInterval(boundary + 1))
        }
        if let left = sessionLeft, left > settings.resetWithinMin * 60 {  // countdown appears in the title
            fire = min(fire, now.addingTimeInterval(left - settings.resetWithinMin * 60 + 1))
        }
        let t = Timer(fire: fire, interval: 0, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.timerFired() }
        }
        t.tolerance = min(currentInterval * 0.05, 5)  // lets macOS coalesce wakeups
        RunLoop.main.add(t, forMode: .common)  // keeps running while the menu is open
        timer = t
    }

    private func timerFired() {
        reloadSettings()
        render()
        updateTicker()
        if Date() >= nextDue.addingTimeInterval(-1) {
            refresh(live: true)
        } else {
            schedule()  // woke at a rule boundary; the new interval isn't due yet
        }
    }

    private func refresh(live: Bool) {
        reloadSettings()
        guard !busy else { return }
        busy = true
        if live { lastAttempt = Date() }
        let path = NSString(string: settings.cmon).expandingTildeInPath
        let args = live ? ["status", "--json", "--live"] : ["status", "--json"]
        Task.detached {
            let result = Self.fetch(path, args)
            await MainActor.run {
                self.busy = false
                switch result {
                case .success(let snap) where snap.offline && self.snapshot != nil:
                    self.error = "Refresh failed — showing last data"  // keep last numbers
                case .success(let snap): self.snapshot = snap; self.error = nil
                case .failure(let err): self.error = err.message
                }
                self.schedule()  // the interval may have changed with the new numbers
                self.render()
                if self.menuOpen { self.menuNeedsUpdate(self.menu) }  // fresh numbers in an open menu
                self.updateTicker()
            }
        }
    }

    struct FetchError: Error { let message: String }

    nonisolated private static func fetch(_ path: String, _ args: [String]) -> Result<Snapshot, FetchError> {
        guard FileManager.default.isExecutableFile(atPath: path) else {
            return .failure(FetchError(message: "cmon not found at \(path) — run make install"))
        }
        let proc = Process(), out = Pipe(), err = Pipe()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = args
        proc.standardOutput = out
        proc.standardError = err
        do { try proc.run() } catch { return .failure(FetchError(message: "cmon failed to start")) }
        // cmon bounds its own network time (status 5 s, --live ~20 s); this is the backstop
        // so a wedged child can never freeze the bar with `busy` stuck on.
        let watchdog = DispatchWorkItem { if proc.isRunning { proc.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 30, execute: watchdog)
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        watchdog.cancel()
        if let snap = try? JSONDecoder().decode(Snapshot.self, from: data) { return .success(snap) }
        if proc.terminationReason == .uncaughtSignal { return .failure(FetchError(message: "cmon timed out")) }
        let detail = String(decoding: errData, as: UTF8.self)
            .split(separator: "\n").last.map(String.init) ?? "cmon status --json unavailable (update cmon)"
        return .failure(FetchError(message: String(detail.prefix(120))))
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
        let title = renderTitle(settings, snap, now: now)
        if button.title != title { button.title = title }  // the ticker calls this every second
        button.appearsDisabled = isStale(snap, now: now) || error != nil
        let usage = snap.windows.map { "\(displayName($0)) \(Int($0.percent.rounded()))%" }
            .joined(separator: "  ·  ")
        button.toolTip = titleLegend(settings).map { "\($0)\n\(usage)" } ?? usage
    }

    // Rebuilt on open; while open, tick() keeps the countdowns and "Updated" line moving.
    func menuNeedsUpdate(_ menu: NSMenu) {
        let now = Date()
        menu.removeAllItems()
        rowViews = [:]
        updatedItem = nil
        menu.addItem(.sectionHeader(title: "Claude"))
        if let legend = titleLegend(settings) {
            let row = NSMenuItem(title: "Title: \(legend)", action: nil, keyEquivalent: "")
            row.isEnabled = false
            menu.addItem(row)
        }
        if let snap = snapshot, !snap.offline {
            for q in snap.windows {
                let row = NSMenuItem()
                let view = QuotaRowView(title: displayName(q), percent: q.percent, detail: resetDetail(q, now: now))
                rowViews[q.key] = view
                row.view = view
                menu.addItem(row)
            }
            menu.addItem(.separator())
            let updated = NSMenuItem(title: updatedTitle(snap, now: now), action: nil, keyEquivalent: "")
            updated.isEnabled = false
            updatedItem = updated
            menu.addItem(updated)
            if isStale(snap, now: now) {
                let hint = NSMenuItem(title: "Data is over \(settings.staleMinutes) min old — refreshes are failing",
                                      action: nil, keyEquivalent: "")
                hint.isEnabled = false
                menu.addItem(hint)
            }
        } else {
            for text in ["Offline — no usage data", "Log in to Claude Code, or check: cmon token status"] {
                let msg = NSMenuItem(title: text, action: nil, keyEquivalent: "")
                msg.isEnabled = false
                menu.addItem(msg)
            }
        }
        if let problem = settingsProblem {
            let msg = NSMenuItem(title: problem, action: nil, keyEquivalent: "")
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
