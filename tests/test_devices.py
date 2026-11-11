import json
import shutil
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import cmon

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
ACCT = "acct-1"


def transcript(home, name, entries):
    d = home / ".claude" / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"uuid": f"{name}-u{n}", "sessionId": sid, "requestId": f"{name}-r{n}",
                         "entrypoint": entry, "timestamp": (T0 + timedelta(minutes=n)).isoformat(),
                         "cwd": f"/w/{name}",
                         "message": {"id": f"{name}-m{n}", "model": "claude-opus-4-1",
                                     "usage": {"input_tokens": 0, "output_tokens": out}}})
             for n, (entry, sid, out) in enumerate(entries)]
    (d / "t.jsonl").write_text("\n".join(lines) + "\n")


class Machine:
    """One device: its own HOME (transcripts, ~/.claude.json, config) and database."""

    def __init__(self, root, monkeypatch, name, account=ACCT):
        self.home, self.monkeypatch, self.name = root / name, monkeypatch, name
        self.home.mkdir()
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account}}))
        self.use()
        cmon._write_config({"device_name": name})

    def use(self):
        mp, h = self.monkeypatch, self.home
        mp.setenv("HOME", str(h))
        mp.delenv("CLAUDE_CONFIG_DIR", raising=False)
        mp.setattr(cmon, "DB", str(h / ".cmon" / "usage.duckdb"))
        mp.setattr(cmon, "CONFIG", str(h / ".cmon" / "config.json"))
        mp.setattr(cmon, "CMON_HOME", str(h / ".cmon"))
        mp.setattr(cmon, "LOGS_ROOT", str(h / ".claude" / "projects"))
        mp.setattr(cmon, "DESKTOP_SESSIONS", str(h / "desktop-sessions"))
        return self

    def sync(self, folder):
        self.use()
        con = cmon.db()
        cmon.scan_logs(con, quiet=True)
        got = cmon.sync_devices(con, str(folder))
        con.close()
        return got

    def by_device(self, capsys, **kw):
        self.use()
        cmon.burn(Namespace(by="device", since="all", json=True, all_accounts=False, plan=None, **kw))
        return {g["grp"]: g["tok"] for g in json.loads(capsys.readouterr().out)}


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    return d


@pytest.fixture
def pair(tmp_path, monkeypatch):
    laptop, desktop = Machine(tmp_path, monkeypatch, "laptop"), Machine(tmp_path, monkeypatch, "desktop")
    transcript(laptop.home, "a", [("cli", "s1", 100), ("claude-vscode", "s2", 20)])
    # a Desktop-app session the laptop tied to the terminal account
    transcript(laptop.home, "b", [("claude-desktop", "d1", 5)])
    sessions = laptop.home / "desktop-sessions" / ACCT / "org"
    sessions.mkdir(parents=True)
    (sessions / "local_d1.json").write_text(json.dumps({"cliSessionId": "d1"}))
    transcript(desktop.home, "c", [("cli", "s3", 1000)])
    return laptop, desktop


def test_two_devices_see_each_other(pair, folder, capsys):
    laptop, desktop = pair
    assert laptop.sync(folder)["exported"] == 3
    got = desktop.sync(folder)
    assert got["exported"] == 1 and got["imported"] == {"laptop": 3}
    assert desktop.by_device(capsys) == {"desktop": 1000, "laptop": 125}  # Desktop session kept
    assert laptop.sync(folder)["imported"] == {"desktop": 1}
    assert laptop.by_device(capsys) == {"desktop": 1000, "laptop": 125}
    assert laptop.by_device(capsys, device="desktop") == {"desktop": 1000}


def test_imported_rows_are_not_exported_again(pair, folder):
    laptop, desktop = pair
    laptop.sync(folder)
    desktop.sync(folder)
    desktop.use()
    con = cmon.db()
    dev = cmon._device_id(con)
    assert con.execute("SELECT count(*) FROM read_parquet(?)", [str(folder / f"{dev}.parquet")]).fetchone()[0] == 1


def test_resync_and_duplicated_file_are_idempotent(pair, folder, capsys):
    laptop, desktop = pair
    laptop.sync(folder)
    desktop.sync(folder)
    second = desktop.sync(folder)
    assert second["exported"] is None and second["imported"] == {}
    laptop.use()
    con = cmon.db()
    dev = cmon._device_id(con)
    con.close()
    shutil.copy(folder / f"{dev}.parquet", folder / f"{dev} 2.parquet")  # what iCloud does on a conflict
    assert desktop.sync(folder)["imported"] == {"laptop": 0}
    assert desktop.by_device(capsys) == {"desktop": 1000, "laptop": 125}


def test_rename_relabels_history(pair, folder, capsys):
    laptop, desktop = pair
    laptop.sync(folder)
    desktop.sync(folder)
    laptop.use()
    cmon._write_config({"device_name": "work-mbp"})
    laptop.sync(folder)
    desktop.sync(folder)
    assert desktop.by_device(capsys) == {"desktop": 1000, "work-mbp": 125}


def test_other_account_is_skipped(tmp_path, monkeypatch, folder, capsys):
    mine, theirs = Machine(tmp_path, monkeypatch, "mine"), Machine(tmp_path, monkeypatch, "theirs", "acct-2")
    transcript(mine.home, "a", [("cli", "s1", 7)])
    transcript(theirs.home, "b", [("cli", "s2", 9)])
    theirs.sync(folder)
    got = mine.sync(folder)
    assert got["other_account"] == ["theirs"] and got["imported"] == {}
    assert mine.by_device(capsys) == {"mine": 7}


def test_unknown_account_refuses(tmp_path, monkeypatch, folder):
    m = Machine(tmp_path, monkeypatch, "m")
    (m.home / ".claude.json").unlink()
    with pytest.raises(RuntimeError, match="account is unknown"):
        m.sync(folder)


def test_sync_command_needs_a_folder(home, cli):
    result = cli("sync")
    assert result.exit_code == 1 and "config set sync_dir" in result.stderr
    result = cli("config", "set", "sync_dir", str(home / "missing"))
    assert result.exit_code == 1 and "not an existing folder" in result.stderr
    result = cli("config", "set", "device_name", "bad/name")
    assert result.exit_code == 1 and "Invalid device_name" in result.stderr


LIMITS = {"limits": [{"kind": "weekly_all", "percent": 3, "resets_at": None, "is_active": True}]}


def test_collect_syncs_when_configured(pair, folder, monkeypatch, capsys):
    laptop, desktop = pair
    laptop.sync(folder)
    desktop.use()
    monkeypatch.setattr(cmon, "fetch", lambda: LIMITS)
    cmon.collect(Namespace(force=True, alert=False))  # no sync_dir yet: nothing exchanged
    assert "synced" not in capsys.readouterr().out
    cmon._write_config({"device_name": "desktop", "sync_dir": str(folder)})
    cmon.collect(Namespace(force=True, alert=False))
    assert "synced 3 new rows from laptop" in capsys.readouterr().out
    assert len(list(folder.glob("*.parquet"))) == 2


def test_collect_survives_a_broken_sync(pair, folder, monkeypatch, capsys):
    _laptop, desktop = pair
    desktop.use()
    cmon._write_config({"sync_dir": str(folder / "gone")})
    monkeypatch.setattr(cmon, "fetch", lambda: LIMITS)
    cmon.collect(Namespace(force=True, alert=False))
    out = capsys.readouterr()
    assert "collected" in out.out and "sync skipped" in out.err
