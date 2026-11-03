// Pure-logic checks for CmonBar.swift (title rendering, refresh rules, settings decoding).
// Run with menubar/test.sh — it compiles this file together with the app minus its @main.

import Foundation

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1 }
    print(ok ? "ok   " : "FAIL ", what)
}

let now = Date()
let m = 60.0
func iso(_ s: Double) -> String { ISO8601DateFormatter().string(from: now.addingTimeInterval(s)) }
func snapshot(session pct: Double, resetIn left: Double) -> Snapshot {
    let json = """
    {"version":1,"offline":false,"observed_at":"\(iso(-12))","age_s":12,"providers":{"claude":{"windows":[
    {"key":"session","label":"Current session","percent":\(pct),"resets_at":"\(iso(left))","active":true},
    {"key":"weekly_all","label":"All models","percent":44.0,"resets_at":"\(iso(91 * 3600 + 900))","active":true}]}}}
    """
    return try! JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
}

// Title: 5h reset only above reset_above_percent; empty slot kept; seconds in the last minute.
let d = Settings()
check(renderTitle(d, snapshot(session: 2, resetIn: 150 * m), now: now) == "2•44••91", "title, 5h reset hidden")
check(renderTitle(d, snapshot(session: 2, resetIn: 99.5 * m), now: now) == "2•44•99•91", "title, shown within 100 min")
check(renderTitle(d, snapshot(session: 90, resetIn: 75.5 * m), now: now) == "90•44•75•91", "title, 5h reset shown")
let lastMinute = renderTitle(d, snapshot(session: 90, resetIn: 42.5), now: now)  // ISO drops sub-seconds
check(lastMinute.hasPrefix("90•44•4") && lastMinute.hasSuffix("s•91"), "title, last minute in seconds")
check(spacedDuration(12) == "12s" && spacedDuration(3700) == "1h 1m", "durations")

// Refresh rules: shortest applicable interval wins.
check(d.refreshInterval(resetLeft: 300 * m, usage: 5) == 600, "default 10 min far from reset")
check(d.refreshInterval(resetLeft: 120 * m, usage: 5) == 180, "within 120 min -> 3 min")
check(d.refreshInterval(resetLeft: 90 * m, usage: 5) == 120, "within 90 min -> 2 min")
check(d.refreshInterval(resetLeft: 30 * m, usage: 5) == 59, "within 60 min -> 59 s")
check(d.refreshInterval(resetLeft: 300 * m, usage: 88) == 59, "usage above 87% -> 59 s")
check(d.refreshInterval(resetLeft: nil, usage: nil) == 600, "no data -> default")
check(d.refreshInterval(resetLeft: -5, usage: 5) == 600, "reset already passed -> default")
check(d.nextBoundary(resetLeft: 125 * m) == 5 * m, "wakes at the 120 min boundary")
check(d.nextBoundary(resetLeft: 30 * m) == nil, "no tighter boundary in the last hour")

// Decoding: partial files keep defaults, bad rules are dropped, intervals are clamped.
let custom = try! JSONDecoder().decode(Settings.self, from: Data("""
{"refresh":[{"every_s":300},{"reset_within_min":30,"every_s":10},{"every_s":-1}],"segments":["{5h}"]}
""".utf8))
check(custom.refresh.count == 2, "invalid every_s dropped")
check(custom.refreshInterval(resetLeft: 10 * m, usage: 1) == RefreshRule.minimumEvery, "clamped to minimum")
check(custom.refreshInterval(resetLeft: 100 * m, usage: 1) == 300, "custom default")
check(custom.separator == "•", "missing keys keep defaults")
let both = try! JSONDecoder().decode(Settings.self, from: Data("""
{"refresh":[{"every_s":600},{"reset_within_min":60,"usage_above_pct":50,"every_s":45}]}
""".utf8))
check(both.refreshInterval(resetLeft: 30 * m, usage: 40) == 600, "conditions combine with AND (one fails)")
check(both.refreshInterval(resetLeft: 30 * m, usage: 60) == 45, "conditions combine with AND (both hold)")

if failures > 0 {
    print("\(failures) failed")
    exit(1)
}
