import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cmon

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
RESET = T0 + timedelta(days=3)
MIN = timedelta(minutes=1)
OPUS = (5.0, 25.0, 0.50, 10.0)
_n = 0


@pytest.fixture
def con(home):
    return cmon.db()


def prices(obj) -> Path:
    path = Path(cmon._prices_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def tok(con, at, surface, out=1_000_000, model="claude-opus-4-1", inp=0):
    global _n
    _n += 1
    con.execute(f"INSERT INTO token_log({cmon._TOKEN_LOG_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", f"p{_n}:r", at, model, surface, "p", "s", inp, out, 0, 0, 0, 0, None])


def snap(con, at, pct):
    con.execute(f"INSERT INTO snapshots({cmon._SNAPSHOT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ["claude", at, "weekly_all", "weekly", "weekly_all", pct, 10080, RESET, True, None, None])


def burn(capsys, **kw):
    args = {"by": "model", "since": "all", "json": True, "all_accounts": False, "plan": None, **kw}
    cmon.burn(Namespace(**args))
    got = capsys.readouterr()
    return (json.loads(got.out) if args["json"] else got.out), got.err


def test_defaults_without_a_file(home, capsys):
    assert cmon._price("claude-opus-4-1") == OPUS
    assert cmon._price_match("claude-mythos-1") is None
    assert cmon._price("claude-mythos-1") == (0.0, 0.0, 0.0, 0.0)
    assert cmon._price(None) == (0.0, 0.0, 0.0, 0.0)
    assert capsys.readouterr().err == ""


def test_file_overrides_and_extends(home, capsys):
    prices({"Opus": [15, 75, 1.5, 30], "mythos": [20, 100, 2, 40]})
    assert cmon._price("claude-opus-4-1") == (15.0, 75.0, 1.5, 30.0)  # keys are case-insensitive
    assert cmon._price_match("claude-mythos-1") == (20.0, 100.0, 2.0, 40.0)
    assert cmon._price("claude-sonnet-4-5") == (3.0, 15.0, 0.30, 6.0)  # built-ins not in the file stay
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("order", [("opus", "opus-4-1"), ("opus-4-1", "opus")])
def test_most_specific_substring_wins_whatever_the_order(home, order):
    table = {"opus": [1, 1, 1, 1], "opus-4-1": [2, 2, 2, 2]}
    prices({k: table[k] for k in order})
    assert cmon._price("claude-opus-4-1-20250805") == (2.0, 2.0, 2.0, 2.0)
    assert cmon._price("claude-opus-4-5") == (1.0, 1.0, 1.0, 1.0)
    keys = [k for k, _p in cmon._price_table()]
    assert keys == sorted(keys, key=lambda k: (-len(k), k))  # same length: alphabetical, deterministic


@pytest.mark.parametrize("content", [
    "{not json",
    '[["opus", 1, 2, 3, 4]]',
    '{"opus": [1, 2, 3]}',
    '{"opus": [1, 2, 3, -4]}',
    '{"opus": "5, 25, 0.5, 10"}',
    '{"opus": [true, 2, 3, 4]}',
    '{"opus": [NaN, 2, 3, 4]}',
    '{"opus": [Infinity, 2, 3, 4]}',
    '{" ": [1, 2, 3, 4]}',
    '{"opus": [1, 2, 3, 4], "OPUS": [1, 2, 3, 4]}',
    '{"opus": [1' + "0" * 400 + ', 2, 3, 4]}',
    b'\xff\xfe{}',
])
def test_invalid_file_warns_once_and_keeps_defaults(home, capsys, content):
    path = Path(cmon._prices_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    for _ in range(3):
        assert cmon._price("claude-opus-4-1") == OPUS
    err = capsys.readouterr().err
    assert err.count("cmon: ignoring") == 1 and "built-in prices" in err and str(path) in err


def test_a_bad_entry_rejects_the_whole_file(home, capsys):
    prices({"mythos": [20, 100, 2, 40], "opus": [1, 2]})
    assert cmon._price_match("claude-mythos-1") is None
    assert "'opus' needs" in capsys.readouterr().err


def test_reloads_when_the_file_changes(home):
    path = prices({"mythos": [1, 1, 1, 1]})
    assert cmon._price("claude-mythos-1") == (1.0, 1.0, 1.0, 1.0)
    prices({"mythos": [20, 100, 2, 40]})
    assert cmon._price("claude-mythos-1") == (20.0, 100.0, 2.0, 40.0)
    path.unlink()
    assert cmon._price_match("claude-mythos-1") is None


def test_burn_text_warns_about_unpriced_models(con, capsys):
    tok(con, T0, "cli")
    tok(con, T0, "cli", model="claude-mythos-1", out=2_000_000)
    tok(con, T0, "cli", model="<synthetic>", out=0)  # no tokens: nothing to warn about
    out, err = burn(capsys, json=False)
    assert "claude-mythos-1" in out and "US$      0.00" in out
    assert "no price for claude-mythos-1 (2.0M tok): its cost counts as US$ 0" in err
    assert "burn --plan gives the percent it used to other groups" in err
    assert cmon._prices_file() in err and "<synthetic>" not in err


def test_burn_names_every_unpriced_model(con, capsys):
    tok(con, T0, "cli", model="claude-mythos-1")
    tok(con, T0, "cli", model="gizmo-2")
    _out, err = burn(capsys, json=False)
    assert "claude-mythos-1 (1.0M tok), gizmo-2 (1.0M tok): their cost" in err and "Add them" in err


def test_burn_quiet_when_everything_is_priced(con, capsys):
    tok(con, T0, "cli")
    prices({"mythos": [20, 100, 2, 40]})
    tok(con, T0, "cli", model="claude-mythos-1")
    out, err = burn(capsys, json=False)
    assert "no price" not in err
    assert "US$    100.00" in out  # 1M output tokens at the file's US$ 100


def test_burn_json_lists_unpriced_models_per_group(con, capsys):
    tok(con, T0, "cli")
    tok(con, T0, "claude-vscode", model="claude-mythos-1")
    got, err = burn(capsys, by="surface")
    by = {g["grp"]: g for g in got}
    assert by["claude-vscode"]["unpriced_models"] == ["claude-mythos-1"] and by["claude-vscode"]["custo"] == 0
    assert "unpriced_models" not in by["cli"]  # additive: priced groups look as before
    assert err == ""  # JSON mode reports it in the data, not on stderr


def test_burn_plan_json_unpriced_usage_goes_elsewhere_until_priced(con, capsys):
    snap(con, T0, 0)
    tok(con, T0 + MIN, "cli")
    tok(con, T0 + MIN, "claude-vscode", model="claude-mythos-1")
    snap(con, T0 + 10 * MIN, 4)
    snap(con, T0 + 11 * MIN, 4)
    tok(con, T0 + 12 * MIN, "claude-vscode", model="claude-mythos-1")
    snap(con, T0 + 20 * MIN, 6)
    got, _err = burn(capsys, by="surface", plan="weekly", since=None)
    assert got["unpriced_models"] == ["claude-mythos-1"]
    pp = {g["grp"]: g["plan_pp"] for g in got["groups"]}
    # priced at zero: the terminal takes the whole first rise, and the second goes to 'other'
    assert pp["cli"] == pytest.approx(4) and pp["claude-vscode"] == 0 and got["other_pp"] == pytest.approx(2)
    prices({"mythos": [0, 25, 0, 0]})  # same output price as Opus
    got, _err = burn(capsys, by="surface", plan="weekly", since=None)
    assert got["unpriced_models"] == []
    pp = {g["grp"]: g["plan_pp"] for g in got["groups"]}
    assert pp["cli"] == pytest.approx(2) and pp["claude-vscode"] == pytest.approx(4) and got["other_pp"] == 0


def test_burn_with_a_bad_file_warns_once(con, capsys):
    tok(con, T0, "cli")
    tok(con, T0, "claude-vscode")
    prices("{oops")
    got, err = burn(capsys, by="surface")
    assert err.count("cmon: ignoring") == 1
    assert all(g["custo"] == pytest.approx(25) for g in got)
