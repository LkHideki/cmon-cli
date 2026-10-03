import json
from argparse import Namespace
from datetime import UTC, datetime

import pytest

import cmon

TERM, OTHER = "acct-terminal", "acct-desktop"
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def accounts(home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": TERM}}))
    sessions = home / "desktop-sessions"
    monkeypatch.setattr(cmon, "DESKTOP_SESSIONS", str(sessions))
    for acct, sid in ((TERM, "d-term"), (OTHER, "d-other")):
        d = sessions / acct / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"local_{sid}.json").write_text(json.dumps({"sessionId": "x", "cliSessionId": sid}))
    lines = []
    for n, (entry, sid) in enumerate((("cli", "c1"), ("claude-vscode", "v1"), ("claude-desktop", "d-term"),
                                      ("claude-desktop", "d-other"), ("claude-desktop", "d-unknown"))):
        lines.append(json.dumps({
            "uuid": f"u{n}", "sessionId": sid, "requestId": f"r{n}", "entrypoint": entry,
            "timestamp": T0.isoformat(), "cwd": "/w/p",
            "message": {"id": f"m{n}", "model": "claude-opus-4-1",
                        "usage": {"input_tokens": 10 ** n, "output_tokens": 0}}}))
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    (d / "t.jsonl").write_text("\n".join(lines) + "\n")
    return home


def test_only_terminal_account_counts(accounts):
    con = cmon.db()
    cmon.scan_logs(con, quiet=True)
    counted = con.execute("SELECT sum(in_tok) FROM token_terminal").fetchone()[0]
    assert counted == 1 + 10 + 100  # cli, vscode, Desktop session tied to the terminal account
    assert con.execute("SELECT sum(in_tok) FROM token_log").fetchone()[0] == 11111  # all kept on record
    assert cmon._burn_rows(con, None)[0][1] == 111


def test_burn_reports_exclusion_and_all_accounts(accounts, capsys):
    args = Namespace(by="surface", since="all", json=False, all_accounts=False)
    cmon.burn(args)
    out = capsys.readouterr().out
    assert "Excluded 0.0M tok" in out and "--all-accounts" in out
    cmon.burn(Namespace(by="surface", since="all", json=True, all_accounts=True))
    assert sum(g["tok"] for g in json.loads(capsys.readouterr().out)) == 11111


def test_unknown_terminal_account_excludes_all_desktop(accounts):
    (accounts / ".claude.json").unlink()
    con = cmon.db()
    cmon.scan_logs(con, quiet=True)
    assert con.execute("SELECT sum(in_tok) FROM token_terminal").fetchone()[0] == 11
