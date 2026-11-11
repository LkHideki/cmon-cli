import json
import shlex
from argparse import Namespace

import pytest

import cmon

RESET = "2030-01-02T08:00:00+00:00"  # 2h after the clock fixture's now
NEXT_RESET = "2030-01-02T13:00:00+00:00"


@pytest.fixture
def sent(monkeypatch):
    """Desktop notifications, as (title, message)."""
    out = []
    monkeypatch.setattr(cmon, "_notify", lambda title, msg: out.append((title, msg)))
    return out


def _config(alert_at):
    cmon._write_config({"alert_at": alert_at})


def _row(pct, reset=RESET, key="session", label="Current session"):
    return [(key, label, float(pct), reset, True)]


@pytest.mark.parametrize(("raw", "normalized"), [
    ("session=80,95 weekly_all=50,75,90", "session=80,95 weekly_all=50,75,90"),
    ("5h=95,80,80   weekly=90", "session=80,95 weekly_all=90"),
    ("WEEK=100 weekly_scoped:Fable=60", "weekly_all=100 weekly_scoped:Fable=60"),
    ("fable=1", "fable=1"),
])
def test_validate_normalizes_aliases_and_percents(raw, normalized):
    assert cmon._validate_alert_at(raw) == normalized


@pytest.mark.parametrize("raw", [
    "", "   ", "session", "session=", "=80", "session=80,,95", "session=0", "session=101",
    "session=80.5", "session=abc", "5h=80 session=90", "weekly=50 WEEKLY_ALL=60", "bad/name=80",
])
def test_validate_rejects_bad_specs(raw):
    with pytest.raises(ValueError, match="Invalid alert_at"):
        cmon._validate_alert_at(raw)


def test_config_set_stores_the_normalized_value(cli, paths):
    result = cli("config", "set", "alert_at", "5h=95,80 weekly=50,75,90")
    assert result.exit_code == 0 and "alert_at = session=80,95 weekly_all=50,75,90" in result.stdout
    assert json.loads(paths.config.read_text()) == {"alert_at": "session=80,95 weekly_all=50,75,90"}


def test_config_set_rejects_an_invalid_value_and_keeps_the_old_one(cli, paths):
    assert cli("config", "set", "alert_at", "session=80").exit_code == 0
    result = cli("config", "set", "alert_at", "session=120")
    assert result.exit_code == 1 and "outside 1-100" in result.stderr
    assert json.loads(paths.config.read_text()) == {"alert_at": "session=80"}


def test_unset_key_fires_nothing_and_writes_no_state(sent, clock):
    cmon._fire_threshold_alerts(_row(99))
    assert sent == [] and not cmon.os.path.exists(cmon.ALERT_STATE)


def test_each_threshold_fires_once_per_cycle(sent, clock, capsys):
    _config("session=80,95")
    cmon._fire_threshold_alerts(_row(79))
    assert sent == []
    for pct in (82, 83, 90):  # three collects past 80 in the same cycle
        cmon._fire_threshold_alerts(_row(pct))
    assert sent == [("cmon — 5h window", "5h window reached 80% (now 82%), resets in ~2h 0m.")]
    assert "⚠ 5h window reached 80% (now 82%)" in capsys.readouterr().err
    cmon._fire_threshold_alerts(_row(96))
    cmon._fire_threshold_alerts(_row(97))
    assert [m for _t, m in sent][1:] == ["5h window reached 95% (now 96%), resets in ~2h 0m."]
    cmon._fire_threshold_alerts(_row(85, reset=NEXT_RESET))  # next cycle re-arms every threshold
    assert sent[-1][1].startswith("5h window reached 80% (now 85%)") and len(sent) == 3


def test_several_crossed_at_once_notify_only_the_highest(sent, clock):
    _config("weekly=50,75,90")
    rows = _row(92, key="weekly_all", label="All models")
    cmon._fire_threshold_alerts(rows)
    cmon._fire_threshold_alerts(_row(93, key="weekly_all", label="All models"))
    assert [m for _t, m in sent] == ["Weekly limit reached 90% (now 92%), resets in ~2h 0m."]


def test_reset_jitter_does_not_refire(sent, clock):
    _config("session=80")
    cmon._fire_threshold_alerts(_row(81, reset="2030-01-02T08:00:00.400000+00:00"))
    cmon._fire_threshold_alerts(_row(82, reset="2030-01-02T07:59:59.700000+00:00"))
    assert len(sent) == 1


def test_windows_are_independent_and_unconfigured_ones_ignored(sent, clock):
    _config("session=80 weekly_all=80")
    rows = [("session", "Current session", 85.0, RESET, True),
            ("weekly_all", "All models", 85.0, NEXT_RESET, True),
            ("weekly_scoped:Fable", "Fable only", 99.0, NEXT_RESET, True)]
    cmon._fire_threshold_alerts(rows)
    assert [t for t, _m in sent] == ["cmon — 5h window", "cmon — Weekly limit"]


def test_scoped_window_matches_by_model_name(sent, clock):
    _config("fable=60")
    cmon._fire_threshold_alerts(_row(61.5, key="weekly_scoped:Fable", label="Fable only"))
    assert sent == [("cmon — Fable only", "Fable only reached 60% (now 61.5%), resets in ~2h 0m.")]


def test_window_without_reset_is_skipped(sent, clock):
    _config("session=1")
    cmon._fire_threshold_alerts(_row(50, reset=None))
    assert sent == [] and not cmon.os.path.exists(cmon.ALERT_STATE)


def test_hook_gets_kind_threshold_and_the_window(sent, clock, tmp_path, monkeypatch):
    out = tmp_path / "hook.log"
    monkeypatch.setenv("CMON_HOOK", "printf '%s\\t%s\\t%s\\t%s\\n' \"$CMON_ALERT_KIND\" \"$CMON_ALERT_KEY\" "
                                    f"\"$CMON_ALERT_PERCENT\" \"$CMON_ALERT_RESETS_AT\" >> {shlex.quote(str(out))}")
    _config("session=80,95")
    cmon._fire_threshold_alerts(_row(96, reset="2030-01-02T05:00:00-03:00"))
    cmon._fire_threshold_alerts(_row(97, reset="2030-01-02T05:00:00-03:00"))
    assert out.read_text().splitlines() == ["threshold\tsession\t96\t2030-01-02T08:00:00+00:00"]


def test_invalid_hand_edited_value_warns_and_fires_nothing(sent, clock, capsys):
    for bad in ("session=eighty", {"session": [80]}):
        _config(bad)
        cmon._fire_threshold_alerts(_row(99))
        assert "ignoring alert_at from config" in capsys.readouterr().err
    assert sent == []


def test_threshold_and_rate_alerts_dedup_separately(sent, clock, monkeypatch):
    _config("session=80")
    rows = _row(90)
    monkeypatch.setattr(cmon, "_rate_alerts", lambda rows, con, tz=None: [("session", RESET, "pace")])
    cmon._fire_rate_alerts(rows, None)
    cmon._fire_threshold_alerts(rows)
    assert [m for _t, m in sent][0] == "pace" and sent[1][1].startswith("5h window reached 80%")
    assert json.load(open(cmon.ALERT_STATE)).keys() == {"rate_session", "threshold_session_80"}


@pytest.mark.parametrize("alert", [True, False])
def test_collect_evaluates_thresholds_only_with_alert(sent, alert, monkeypatch):
    monkeypatch.setattr(cmon, "_rate_alerts", lambda rows, con, tz=None: [])
    reset = (cmon.datetime.now(cmon.UTC) + cmon.timedelta(hours=2)).isoformat()
    monkeypatch.setattr(cmon, "fetch", lambda: {"limits": [
        {"kind": "session", "percent": 88, "resets_at": reset, "is_active": True}]})
    _config("5h=80")
    cmon.collect(Namespace(force=True, alert=alert))
    assert [m.split(" (")[0] for _t, m in sent] == (["5h window reached 80%"] if alert else [])
