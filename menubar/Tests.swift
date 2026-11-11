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

// Default title: two used-and-left pairs, both always visible, each in its best unit.
let d = Settings()
check(renderTitle(d, snapshot(session: 2, resetIn: 150 * m), now: now) == "2% 2  44% 91", "title, hours from 2 h up")
check(renderTitle(d, snapshot(session: 90, resetIn: 75.5 * m), now: now) == "90% 75'  44% 91", "title, minutes below 2 h")
check(pairTime(7200) == "2" && pairTime(7199) == "119'", "unit changes exactly at 2 h")
check(pairTime(42) == "42\"" && pairTime(60) == "1'", "seconds only in the last minute")
check(usagePair(percent: 100, left: 600 * 60) == "100% 10", "three digits")
check(usagePair(percent: nil, left: nil) == "– –", "unknown values")
check(renderTitle(d, snapshot(session: 90, resetIn: -5), now: now).hasPrefix("90% –"), "reset passed shows no time")
check(unitDuration(1800, hours: true) == "30m" && unitDuration(7300, hours: true) == "2h", "week reset under 1 h keeps a real unit")
check(unitDuration(7300, hours: false) == "121m", "5h reset stays in minutes")
check(titleLegend(d) != nil, "default layout is explained in the menu")

// Bare placeholders keep working for layouts already saved in bar.json.
var bare = Settings()
bare.segments = ["{5h}", "{wk}", "{reset}", "{wk_reset_h}"]
bare.separator = "•"
check(renderTitle(bare, snapshot(session: 90, resetIn: 75.5 * m), now: now) == "90•44•75•91", "bare layout, reset shown")
check(renderTitle(bare, snapshot(session: 2, resetIn: 150 * m), now: now) == "2•44••91", "bare layout, reset hidden")
check(titleLegend(bare) == nil, "custom layout has no legend")
check(spacedDuration(12) == "12s" && spacedDuration(3700) == "1h 1m", "durations")

// hide_empty: a blank placeholder takes its word's decoration along; empty segments vanish.
var labelled = Settings()
labelled.hideEmpty = true
labelled.separator = " · "
labelled.segments = ["5h {5h_pct} {reset_short}", "7d {wk_pct} {wk_reset_short}"]
check(renderTitle(labelled, snapshot(session: 2, resetIn: 150 * m), now: now) == "5h 2% · 7d 44% 91h", "labelled, reset hidden")
check(renderTitle(labelled, snapshot(session: 90, resetIn: 75.5 * m), now: now) == "5h 90% 75m · 7d 44% 91h", "labelled, reset shown")
labelled.segments = ["{5h_pct}", "↻{reset_short}", "{wk_pct}"]
check(renderTitle(labelled, snapshot(session: 2, resetIn: 150 * m), now: now) == "2% · 44%", "decoration leaves with its value")
check(usageBar(0) == "▁" && usageBar(50) == "▅" && usageBar(100) == "█", "usage blocks stay in range")
check(usageDot(79) == "🟢" && usageDot(80) == "🟡" && usageDot(95) == "🔴", "traffic light thresholds")
let hideOff = try! JSONDecoder().decode(Settings.self, from: Data("{}".utf8))
check(!hideOff.hideEmpty, "hide_empty is off unless asked")

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
check(custom.separator == Settings.defaultSeparator, "missing keys keep defaults")
let both = try! JSONDecoder().decode(Settings.self, from: Data("""
{"refresh":[{"every_s":600},{"reset_within_min":60,"usage_above_pct":50,"every_s":45}]}
""".utf8))
check(both.refreshInterval(resetLeft: 30 * m, usage: 40) == 600, "conditions combine with AND (one fails)")
check(both.refreshInterval(resetLeft: 30 * m, usage: 60) == 45, "conditions combine with AND (both hold)")

// By client: optional in the JSON, drives {top_client} and the menu legend.
let splitJSON = """
{"version":1,"offline":false,"observed_at":"\(iso(-12))","age_s":12,"providers":{"claude":{"windows":[
{"key":"session","label":"Current session","percent":10,"resets_at":"\(iso(3600))","active":true},
{"key":"weekly_all","label":"All models","percent":80,"resets_at":"\(iso(86400))","active":true,
 "by_client":{"computed_at":"\(iso(-600))","other":0.1,"clients":[
  {"key":"cli","label":"Terminal","share":0.3},{"key":"claude-vscode","label":"VS Code","share":0.6}]}}]}}}
"""
let split = try! JSONDecoder().decode(Snapshot.self, from: Data(splitJSON.utf8))
let week = split.window("weekly_all")!
check(topClient(week) == "VS Code 60%", "top client of the week")
check(topClient(split.window("session")) == "" && clientLines(split.window("session")!).isEmpty,
      "no split, nothing shown")
let lines = clientLines(week)
check(lines.map(\.label) == ["VS Code", "Terminal", "claude.ai & elsewhere"], "legend order, other last")
check(abs(lines[0].points - 48) < 1e-9 && abs(lines[2].points - 8) < 1e-9, "points of the limit")
check(splitCaption(week, now: now) == "80% used · split 10 min ago", "caption says how old the split is")
var topTitle = Settings()
topTitle.hideEmpty = true
topTitle.segments = ["{wk_pct}", "{5h_top_client}", "{top_client}"]
check(renderTitle(topTitle, split, now: now) == "80%  VS Code 60%", "top client placeholder, blank one hidden")
check(snapshot(session: 2, resetIn: 150 * m).window("weekly_all")?.byClient == nil, "older JSON still decodes")

// Projection: optional fields from `cmon collect`, drive {5h_proj} {wk_proj} {5h_eta} and the menu line.
func projected(session: String, weekly: String) -> Snapshot {
    let json = """
    {"version":1,"offline":false,"observed_at":"\(iso(-12))","age_s":12,"providers":{"claude":{"windows":[
    {"key":"session","label":"Current session","percent":50,"resets_at":"\(iso(4 * 3600))","active":true\(session)},
    {"key":"weekly_all","label":"All models","percent":30,"resets_at":"\(iso(50 * 3600))","active":true\(weekly)}]}}}
    """
    return try! JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
}
let over = projected(
    session: #","projected_percent":130.4,"exhausts_at":"\#(iso(80 * m + 30))","projection_at":"\#(iso(-30))""#,
    weekly: #","projected_percent":79.6,"exhausts_at":null,"projection_at":"\#(iso(-30))""#)
var projTitle = Settings()
projTitle.segments = ["{5h_pct} →{5h_proj} {5h_eta}", "{wk_proj}"]
check(renderTitle(projTitle, over, now: now) == "50% →130% 80m  80%", "projection placeholders")
check(over.window("weekly_all")?.exhaustsAt == nil && exhaustsIn(over.window("weekly_all"), now: now) == nil,
      "null exhausts_at decodes, no eta")
check(projectionDetail(over.window("session")!, now: now) == "Projected 130% · 100% in 1h 20m",
      "menu line past 100%")
check(projectionDetail(over.window("weekly_all")!, now: now) == "Projected 80% at reset", "menu line under 100%")
check(projectionDetail(over.window("session")!, now: now.addingTimeInterval(10 * m))
      == "Projected 130% · 100% in 1h 10m · 10 min ago", "menu line says how old the projection is")
check(projectionDetail(over.window("session")!, now: now.addingTimeInterval(2 * 3600))
      == "Projected 130% at reset · 2h 0m ago", "a lapsed exhaustion time is dropped")
check(exhaustsIn(over.window("session"), now: now.addingTimeInterval(81 * m)) == nil, "eta in the past is blank")
let none = snapshot(session: 50, resetIn: 4 * 3600)
check(none.window("session")?.projectedPercent == nil && projectionDetail(none.window("session")!, now: now) == nil,
      "older JSON decodes without a projection, no menu line")
check(renderTitle(projTitle, none, now: now) == "50% →   ", "unknown projection is blank")
projTitle.hideEmpty = true
check(renderTitle(projTitle, none, now: now) == "50%", "blank projection words hidden")
let inconsistent = projected(session: #","projected_percent":95,"exhausts_at":"\#(iso(3600))""#, weekly: "")
check(renderTitle(projTitle, inconsistent, now: now) == "50% →95%", "no eta while the projection is under 100%")

if failures > 0 {
    print("\(failures) failed")
    exit(1)
}
