import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from argparse import Namespace
from datetime import UTC, timedelta

import pytest

import cmon

SECRET = "sk-ant-oat01-doctor-must-never-print-this-ZqXw"
KEYS = ["token", "api", "schedule", "lock", "schema", "snapshot", "sync_dir", "config", "bar"]


class Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def _no_fetch(*_a, **_k):
    raise AssertionError("doctor --offline must not call the API")


@pytest.fixture
def doctor_env(monkeypatch):
    """A token in the vault, no API call, and no scheduler tool reachable."""
    monkeypatch.setattr(cmon, "_resolve_token", lambda: ("OS vault (test)", SECRET, None))
    monkeypatch.setattr(cmon, "fetch", _no_fetch)
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: None)


def _by_key(checks):
    return {c["key"]: c for c in checks}


def _save_snapshot(at):
    con = cmon.db()
    con.executemany(
        f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        cmon._claude_snapshot_records([("session", "Current session", 10.0, None, True)], at),
    )
    con.close()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --- the command ---

def test_json_report_lists_every_check_and_never_the_token(doctor_env, cli):
    res = cli("doctor", "--offline", "--json")
    assert res.exit_code == 0
    report = json.loads(res.stdout)
    assert report["version"] == 1 and report["ok"] is True
    assert [c["key"] for c in report["checks"]] == KEYS
    assert all(set(c) == {"key", "name", "status", "detail", "fix"} for c in report["checks"])
    checks = _by_key(report["checks"])
    assert checks["token"]["status"] == "ok" and checks["token"]["detail"] == "OS vault (test)"
    assert checks["api"]["status"] == "skip"
    assert SECRET not in res.stdout and SECRET[:12] not in res.stdout and SECRET[-4:] not in res.stdout


def test_names_make_doctor_and_agents_md_document(doctor_env, capsys):
    cmon.doctor(Namespace(offline=True, json=True))
    checks = _by_key(json.loads(capsys.readouterr().out)["checks"])
    assert checks["api"]["name"] == "usage API reachable and logged in"
    assert checks["bar"]["name"] == "bar.json is valid"


def test_any_fail_exits_1_and_prints_the_fix(doctor_env, cli, paths):
    _write(str(paths.home / ".cmon" / "bar.json"), "{not json")
    res = cli("doctor", "--offline")
    assert res.exit_code == 1
    lines = res.stdout.splitlines()
    i = lines.index("  FAIL  bar.json is valid")
    assert lines[i + 1].startswith("        not valid JSON")
    assert lines[i + 2].startswith("        -> fix ") and "delete it to get the defaults" in lines[i + 2]
    assert lines[-1] == "1 problem(s) found, 2 warning(s)."


def test_warnings_alone_exit_0(doctor_env, cli):
    res = cli("doctor", "--offline")  # no schedule, no database: warnings, not failures
    assert res.exit_code == 0
    assert "  warn  background collection scheduled" in res.stdout
    assert "  skip  usage API reachable and logged in (--offline)" in res.stdout
    assert res.stdout.rstrip().endswith("No problems, 2 warning(s).")


def test_all_good_summary(doctor_env, cli, monkeypatch, clock):
    monkeypatch.setattr(cmon, "_doctor_schedule", lambda: cmon._check("schedule", "background", "ok", "x"))
    _save_snapshot(cmon.datetime.now(UTC) - timedelta(minutes=5))
    res = cli("doctor", "--offline")
    assert res.exit_code == 0 and res.stdout.rstrip().endswith("All good.")
    assert "  ok    last snapshot is recent (5m ago)" in res.stdout


# --- token ---

def test_no_token_fails_with_login_fix(monkeypatch):
    monkeypatch.setattr(cmon, "_resolve_token", lambda: (None, None, None))
    c = cmon._doctor_token()
    assert c["status"] == "fail" and "cmon token set" in c["fix"]


def test_expired_claude_code_token_warns(monkeypatch):
    monkeypatch.setattr(cmon, "_resolve_token", lambda: (cmon.CC_SOURCE, SECRET, int((time.time() - 60) * 1000)))
    c = cmon._doctor_token()
    assert c["status"] == "warn" and "expired" in c["detail"] and SECRET not in json.dumps(c)


def test_token_expiry_and_env_source_are_explained(monkeypatch):
    monkeypatch.setattr(cmon, "_resolve_token", lambda: (cmon.CC_SOURCE, SECRET, int((time.time() + 7200) * 1000)))
    assert cmon._doctor_token()["detail"].startswith("Claude Code credentials, expires in 1h 5")
    monkeypatch.setattr(cmon, "_resolve_token", lambda: ("env CLAUDE_OAUTH_TOKEN", SECRET, None))
    c = cmon._doctor_token()
    assert c["status"] == "ok" and "doesn't inherit" in c["detail"]


def test_token_lookup_crash_is_a_failure_not_a_traceback(monkeypatch):
    def boom():
        raise RuntimeError("vault exploded")
    monkeypatch.setattr(cmon, "_resolve_token", boom)
    assert cmon._doctor_token()["status"] == "fail"


# --- API ---

def test_api_ok_lists_the_windows(monkeypatch):
    seen = {}

    def fake(retries, timeout):
        seen.update(retries=retries, timeout=timeout)
        return {"limits": [{"kind": "session", "percent": 12.4}, {"kind": "weekly_all", "percent": 40}]}
    monkeypatch.setattr(cmon, "fetch", fake)
    c = cmon._doctor_api(offline=False)
    assert c["status"] == "ok" and c["detail"] == "Current session 12%, All models 40%"
    assert seen == {"retries": 2, "timeout": 10}  # the budget 'status --live' had in make doctor
    assert not os.path.exists(cmon.CACHE)  # a diagnosis stores nothing


@pytest.mark.parametrize(("msg", "fix"), [
    ("401 — token from Claude Code credentials was rejected.", "run Claude Code once to log in"),
    ("no token — log in to Claude Code", "run Claude Code once to log in"),
    ("Claude Code's access token has expired", "run Claude Code once to log in"),
    ("403 — blocked (Cloudflare/User-Agent) or no access.", "uv tool upgrade cmon-cli"),
    ("Failed to query x after 2 attempts (network: down).", "check the network"),
])
def test_api_failure_picks_the_fix_by_cause(monkeypatch, msg, fix):
    def fail(**_k):
        raise cmon.FetchError(msg)
    monkeypatch.setattr(cmon, "fetch", fail)
    c = cmon._doctor_api(offline=False)
    assert c["status"] == "fail" and c["detail"] == msg and fix in c["fix"]


def test_api_reply_of_unknown_shape_fails(monkeypatch):
    monkeypatch.setattr(cmon, "fetch", lambda **_k: {"limits": [{"percent": 1}]})  # no 'kind'
    c = cmon._doctor_api(offline=False)
    assert c["status"] == "fail" and c["detail"].startswith("unexpected reply")


def test_offline_skips_the_api(monkeypatch):
    monkeypatch.setattr(cmon, "fetch", _no_fetch)
    assert cmon._doctor_api(offline=True)["status"] == "skip"


# --- background collection ---

def _launch_agent(paths, argv, every=1200):
    plist = paths.home / "Library" / "LaunchAgents" / f"{cmon.AGENT}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": cmon.AGENT, "ProgramArguments": argv, "StartInterval": every}))


def _sched_argv(db=None):
    return [sys.executable, cmon.__file__, "--db", db or os.path.abspath(cmon.DB), "collect", "--alert"]


def test_macos_not_scheduled_warns(paths, monkeypatch):
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: pytest.fail("no LaunchAgent, nothing to ask launchctl"))
    c = cmon._doctor_schedule("darwin")
    assert c["status"] == "warn" and c["fix"] == "cmon install"


def test_macos_loaded_agent_is_ok(paths, monkeypatch):
    _launch_agent(paths, _sched_argv())
    calls = []
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: calls.append(cmd) or Proc(0))
    c = cmon._doctor_schedule("darwin")
    assert c["status"] == "ok" and c["detail"] == f"LaunchAgent {cmon.AGENT}, every 20 min"
    assert calls == [["launchctl", "print", f"gui/{os.getuid()}/{cmon.AGENT}"]]


def test_macos_agent_not_loaded_warns(paths, monkeypatch):
    _launch_agent(paths, _sched_argv())
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(113))
    c = cmon._doctor_schedule("darwin")
    assert c["status"] == "warn" and "isn't loaded" in c["detail"]


def test_macos_agent_pointing_at_a_deleted_clone_fails(paths, monkeypatch):
    gone = str(paths.root / "deleted-clone" / "cmon.py")
    _launch_agent(paths, [sys.executable, gone, "--db", os.path.abspath(cmon.DB), "collect"])
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(0))
    c = cmon._doctor_schedule("darwin")
    assert c["status"] == "fail" and gone in c["detail"] and c["fix"] == "cmon install"


def test_macos_agent_feeding_another_database_warns(paths, monkeypatch):
    other = str(paths.root / "other.duckdb")
    _launch_agent(paths, _sched_argv(other))
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(0))
    c = cmon._doctor_schedule("darwin")
    assert c["status"] == "warn" and other in c["detail"]


def test_macos_unreadable_plist_fails(paths, monkeypatch):
    plist = paths.home / "Library" / "LaunchAgents" / f"{cmon.AGENT}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("garbage")
    assert cmon._doctor_schedule("darwin")["status"] == "fail"


def test_macos_without_launchctl_warns(paths, monkeypatch):
    _launch_agent(paths, _sched_argv())
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: None)
    assert cmon._doctor_schedule("darwin")["status"] == "warn"


def _systemd(paths, argv):
    d = paths.home / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / "cmon-collect.timer").write_text("[Timer]\nOnUnitActiveSec=20min\n")
    (d / "cmon-collect.service").write_text(f"[Service]\nType=oneshot\nExecStart={shlex.join(argv)}\n")


@pytest.mark.parametrize(("state", "status"), [("active\n", "ok"), ("inactive\n", "warn")])
def test_linux_systemd_timer(paths, monkeypatch, state, status):
    _systemd(paths, _sched_argv())
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(0 if status == "ok" else 3, state))
    assert cmon._doctor_schedule("linux")["status"] == status


def test_linux_systemd_service_with_missing_program_fails(paths, monkeypatch):
    _systemd(paths, ["/nonexistent/python3", cmon.__file__, "collect"])
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(0, "active\n"))
    c = cmon._doctor_schedule("linux")
    assert c["status"] == "fail" and "/nonexistent/python3" in c["detail"]


def test_linux_cron_entry_is_ok_and_absent_warns(paths, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    line = f"*/20 * * * * {shlex.join(_sched_argv())}  # cmon-collect"
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(0, f"MAILTO=x\n{line}\n"))
    assert cmon._doctor_schedule("linux")["status"] == "ok"
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: Proc(1, ""))  # no crontab at all
    c = cmon._doctor_schedule("linux")
    assert c["status"] == "warn" and c["detail"] == "not scheduled"


@pytest.mark.parametrize(("proc", "status"), [(Proc(0), "ok"), (Proc(1), "warn"), (None, "warn")])
def test_windows_task(monkeypatch, proc, status):
    calls = []
    monkeypatch.setattr(cmon, "_doctor_run", lambda cmd: calls.append(cmd) or proc)
    assert cmon._doctor_schedule("win32")["status"] == status
    assert calls == [["schtasks", "/query", "/tn", "cmon-collect"]]


def test_unknown_platform_skips():
    assert cmon._doctor_schedule("freebsd14")["status"] == "skip"


def test_doctor_run_survives_a_missing_tool():
    assert cmon._doctor_run(["/nonexistent/tool-for-doctor"]) is None


# --- database ---

def test_no_database_yet(paths):
    lock, schema, snap = cmon._doctor_db()
    assert (lock["status"], schema["status"], snap["status"]) == ("skip", "skip", "warn")
    assert not paths.db.exists()  # doctor never creates it


def test_fresh_database_is_ok(paths, clock):
    _save_snapshot(cmon.datetime.now(UTC) - timedelta(minutes=12))
    checks = _by_key(cmon._doctor_db())
    assert [checks[k]["status"] for k in ("lock", "schema", "snapshot")] == ["ok", "ok", "ok"]
    assert checks["schema"]["detail"] == f"v{cmon.SCHEMA_VERSION}"
    assert checks["snapshot"]["detail"] == "12m ago"


def test_stale_or_missing_snapshot_warns(paths, clock):
    cmon.db().close()
    assert _by_key(cmon._doctor_db())["snapshot"]["detail"] == "no snapshots yet"
    _save_snapshot(cmon.datetime.now(UTC) - timedelta(hours=3))
    snap = _by_key(cmon._doctor_db())["snapshot"]
    assert snap["status"] == "warn" and snap["detail"] == "3h 0m ago" and "cmon collect" in snap["fix"]


def _set_version(value):
    con = cmon.db()
    con.execute("UPDATE meta SET value=? WHERE key='schema_version'", [value])
    con.close()


def test_older_schema_warns_and_doctor_never_migrates_it(paths):
    _set_version("1")
    schema = _by_key(cmon._doctor_db())["schema"]
    assert schema["status"] == "warn" and "migrates it to v2" in schema["detail"]
    assert "copy" in schema["fix"]
    import duckdb
    con = duckdb.connect(str(paths.db), read_only=True)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("1",)
    con.close()


@pytest.mark.parametrize(("value", "needle"), [("99", "newer than this cmon"), ("two", "invalid schema version")])
def test_newer_or_garbled_schema_fails(paths, value, needle):
    _set_version(value)
    schema = _by_key(cmon._doctor_db())["schema"]
    assert schema["status"] == "fail" and needle in schema["detail"]


def test_schema_missing_columns_fails(paths):
    con = cmon.db()
    con.execute("ALTER TABLE snapshots DROP COLUMN source_event_id")
    con.close()
    schema = _by_key(cmon._doctor_db())["schema"]
    assert schema["status"] == "fail" and "source_event_id" in schema["detail"]


def test_unversioned_database_warns(paths):
    con = cmon.db()
    con.execute("DELETE FROM meta WHERE key='schema_version'")
    con.close()
    assert _by_key(cmon._doctor_db())["schema"]["status"] == "warn"


def test_file_that_isnt_a_database_fails(paths):
    paths.db.write_bytes(b"this is not a duckdb file\n" * 200)
    lock, schema, snap = cmon._doctor_db()
    assert (lock["status"], schema["status"], snap["status"]) == ("skip", "fail", "skip")


HOLD = """
import sys, time, duckdb
con = duckdb.connect(sys.argv[1])
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""


def test_locked_database_warns_with_the_holder(paths, monkeypatch):
    cmon.db().close()
    monkeypatch.setattr(cmon, "DOCTOR_LOCK_WAIT_S", 0.2)
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(paths.db), "5"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        lock, schema, snap = cmon._doctor_db()
    finally:
        holder.kill()
        holder.wait()
    assert lock["status"] == "warn" and f"PID {holder.pid}" in lock["detail"]
    assert (schema["status"], snap["status"]) == ("skip", "skip")


# --- sync_dir ---

def test_sync_dir_states(paths):
    assert cmon._doctor_sync_dir({})["status"] == "skip"
    assert cmon._doctor_sync_dir({"sync_dir": str(paths.root)}) == cmon._check(
        "sync_dir", "sync_dir reachable", "ok", str(paths.root))
    gone = cmon._doctor_sync_dir({"sync_dir": str(paths.root / "unmounted")})
    assert gone["status"] == "fail" and "clear sync_dir" in gone["fix"]
    assert cmon._doctor_sync_dir({"sync_dir": 7})["status"] == "fail"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root writes anywhere")
def test_read_only_sync_dir_fails(paths):
    ro = paths.root / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert "not writable" in cmon._doctor_sync_dir({"sync_dir": str(ro)})["detail"]
    finally:
        ro.chmod(0o700)


# --- config.json and bar.json ---

def _config(paths, text):
    _write(str(paths.config), text)
    return cmon._doctor_config()


def test_config_absent_or_valid_is_ok(paths):
    assert cmon._doctor_config()["status"] == "ok"
    c = _config(paths, json.dumps({"timezone": "UTC", "alert_at": "session=80", "sync_dir": "/gone"}))
    assert c["status"] == "ok"  # a missing sync_dir folder is the sync_dir check's failure, not this one


@pytest.mark.parametrize(("text", "needle"), [
    ("{", "not valid JSON"),
    ("[1, 2]", "not a JSON object"),
    ('{"timezone": "Mars/Base"}', "Unknown timezone 'Mars/Base'"),
    ('{"keepalive": true}', "keepalive: True is not a non-empty string"),
    ('{"alert_at": "session=120", "zzz": 1}', "unknown keys, ignored: zzz"),
])
def test_config_problems_fail(paths, text, needle):
    c = _config(paths, text)
    assert c["status"] == "fail" and needle in c["detail"]


def test_config_unknown_keys_alone_warn(paths):
    c = _config(paths, '{"timezone": "UTC", "colour": "blue"}')
    assert c["status"] == "warn" and "colour" in c["detail"]


def _bar(paths, text):
    _write(str(paths.home / ".cmon" / "bar.json"), text)
    return cmon._doctor_bar()


def test_bar_absent_or_valid_is_ok(paths):
    assert cmon._doctor_bar()["status"] == "ok"
    valid = {"segments": ["{5h_pair}", "{wk_pair}"], "separator": "  ", "hide_empty": False,
             "reset_above_percent": 85, "reset_within_min": 100.0, "stale_minutes": 30.0, "cmon": "~/x",
             "watch_command": "x", "refresh": [{"every_s": 600}, {"reset_within_min": 60, "every_s": 59,
                                                                  "usage_above_pct": None}],
             "unknown_key": [1], "segments_old": None}
    assert _bar(paths, json.dumps(valid))["status"] == "ok"
    assert _bar(paths, '{"segments": null, "refresh": null}')["status"] == "ok"  # null = default, as in the app


@pytest.mark.parametrize(("text", "needle"), [
    ("{nope", "not valid JSON"),
    ("[]", "not a JSON object"),
    ('{"stale_minutes": NaN}', "NaN is not valid JSON"),
    ('{"segments": ["{5h}", 1]}', "segments must be a list of strings"),
    ('{"hide_empty": 1}', "hide_empty must be true or false"),
    ('{"reset_above_percent": true}', "reset_above_percent must be a number"),
    ('{"stale_minutes": 30.5}', "stale_minutes must be a whole number"),
    ('{"stale_minutes": 99999999999999999999}', "stale_minutes must be a whole number"),
    ('{"refresh": [{"reset_within_min": 5}]}', "refresh must be a list of rules"),
    ('{"refresh": [{"every_s": "60"}]}', "refresh must be a list of rules"),
    ('{"separator": 5, "cmon": false}', "separator must be a string; cmon must be a string"),
])
def test_bar_problems_fail_like_the_app_decoder(paths, text, needle):
    c = _bar(paths, text)
    assert c["status"] == "fail" and needle in c["detail"] and "delete it to get the defaults" in c["fix"]
