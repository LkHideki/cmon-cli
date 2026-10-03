import os
import stat
import subprocess
import sys

import cmon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_in(cwd, home, env_extra=None):
    """Fresh interpreter importing cmon from `cwd` with HOME=home; returns selected env vars."""
    code = (f"import os, sys; sys.path.insert(0, {ROOT!r}); import cmon; "
            "print(os.environ.get('CMON_HOOK', '-')); print(os.environ.get('CMON_TZ', '-'))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CMON_")}
    env.update(HOME=str(home), **(env_extra or {}))
    out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True)
    return out.stdout.split(), out.stderr


def test_dotenv_in_cwd_is_ignored(tmp_path):
    repo = tmp_path / "some-repo"
    repo.mkdir()
    (repo / ".env").write_text("CMON_HOOK=touch /tmp/pwned\n")
    assert _import_in(repo, tmp_path)[0] == ["-", "-"]


def test_dotenv_in_cmon_home_applies_and_env_wins(tmp_path):
    (tmp_path / ".cmon").mkdir()
    env_file = tmp_path / ".cmon" / ".env"
    env_file.write_text("CMON_HOOK=notify-send hi\nCMON_TZ=UTC\n")
    env_file.chmod(0o600)
    assert _import_in(tmp_path, tmp_path, {"CMON_TZ": "Asia/Tokyo"})[0] == ["notify-send", "hi", "Asia/Tokyo"]


def test_group_writable_dotenv_is_refused(tmp_path):
    (tmp_path / ".cmon").mkdir()
    env_file = tmp_path / ".cmon" / ".env"
    env_file.write_text("CMON_HOOK=evil\n")
    env_file.chmod(0o664)
    values, err = _import_in(tmp_path, tmp_path)
    assert values == ["-", "-"] and "writable by others" in err


def test_children_never_see_cmon_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "secret")
    env = cmon._child_env(CMON_ALERT_MSG="hi")
    assert "CLAUDE_OAUTH_TOKEN" not in env and env["CMON_ALERT_MSG"] == "hi" and env["PATH"]


def test_ai_tip_runs_sandboxed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "secret")
    monkeypatch.setattr(cmon, "CMON_HOME", str(tmp_path / ".cmon"))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="tip", stderr="")

    monkeypatch.setattr(cmon.subprocess, "run", fake_run)
    assert cmon._ai_tip("5h 40%") == "tip"
    assert seen["cwd"] == str(tmp_path / ".cmon" / "ai") and os.listdir(seen["cwd"]) == []
    assert "CLAUDE_OAUTH_TOKEN" not in seen["env"]
    assert stat.S_IMODE(os.stat(seen["cwd"]).st_mode) == 0o700


def test_private_dir_tightens_existing(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o755)
    cmon._private_dir(str(d))
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
