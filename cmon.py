"""cmon — Claude Monitor. Track your Claude plan consumption over time.

Source: private endpoint https://claude.ai/api/oauth/usage (same as the app uses).
Token, resolved in order:
  1. CLAUDE_OAUTH_TOKEN environment variable (useful in CI / override);
  2. OS secure vault — Keychain (macOS), Credential Manager (Windows) or
     Secret Service (Linux) —, saved once with `cmon token set`;
  3. Claude Code credentials, if you're logged in (zero friction).
When the access token expires, cmon automatically renews it via refresh_token and stores
the new string in its vault (doesn't re-save Claude Code credentials). If an old
CLAUDE_OAUTH_TOKEN returns 401, cmon refreshes and uses the new one anyway.

  cmon now         # current usage + reset + rate/projection (--advice adds pacing tips)
  cmon collect     # save 1 snapshot to database (run via cron every ~20min)
  cmon trends      # consumption history: per-label summary + per-cycle peaks/anomaly
  cmon plot        # charts -> PNG: trajectory, pace-vs-target, burn by model
  cmon backtest    # how accurate past projections were (--replay scores existing history)
  cmon tune        # fit the projection's parameters to your own history
  cmon token set   # save token to OS secure vault (cross-platform)
"""

import argparse
import bisect
import getpass
import itertools
import json
import os
import subprocess
import sys
from collections import namedtuple
from datetime import UTC, datetime, timedelta

# One cycle of a window's snapshots (segmented at each reset). Native replacement
# for the pandas DataFrame slices _cycles() used to return: .ts is a list of
# tz-aware datetimes, .percent a list of floats.
Cycle = namedtuple("Cycle", "ts percent")

# Fixed home, not cwd: a relative default split history across every directory cmon ran in.
DB = os.environ.get("CMON_DB") or os.path.expanduser("~/.cmon/usage.duckdb")
URL = "https://claude.ai/api/oauth/usage"
# UA is required: without it claude.ai's Cloudflare returns 403 ("Just a moment").
UA = "claude-cli/1.0 (external, cli)"
LABELS = {"session": "Current session", "weekly_all": "All models"}
SERVICE, ACCOUNT = "cmon", "claude-oauth"  # entry in the OS secure vault (manual token set)
AUTO_ACCOUNT = "claude-oauth-auto"  # chain refreshed by cmon, separate from Claude Code
RETRIES = int(os.environ.get("CMON_RETRIES", "3"))
DEDUP_SECS = int(os.environ.get("CMON_DEDUP_SECS", "60"))  # window to deduplicate collect
ALERT_LEAD_MIN = int(os.environ.get("CMON_ALERT_LEAD", "60"))  # minutes-before-reset to alert (5h window)
ALERT_LEAD_WEEKLY_MIN = int(os.environ.get("CMON_ALERT_LEAD_WEEKLY", "180"))  # same, for the weekly window
# Per-window lead (minutes-before-reset) for the time-based alert. Extend here to cover more windows.
LEAD_MIN_BY_KEY = {"session": ALERT_LEAD_MIN, "weekly_all": ALERT_LEAD_WEEKLY_MIN}
LEAD_LABEL_BY_KEY = {"session": "5h window", "weekly_all": "Weekly limit"}
MAX_JSONL_LINE = 4 << 20  # 4 MiB: skip parsing absurdly large transcript lines (secperf F4 DoS guard)
# Hourly-profile learning (see _hour_profile). Only *dense* snapshot pairs shape the hour
# histogram: a wide gap can't say when inside it the usage happened, and spreading it evenly
# invents consumption at 4am. Recency decays instead of being truncated so the shape drifts
# with your routine rather than jumping when a day leaves a fixed window.
PROFILE_PAIR_MAX_H = float(os.environ.get("CMON_PROFILE_PAIR_MAX_H", "1.5"))  # gap above this = ignorance
PROFILE_HALFLIFE_D = float(os.environ.get("CMON_PROFILE_HALFLIFE_D", "10"))   # weight half-life
PROFILE_MAX_AGE_D = float(os.environ.get("CMON_PROFILE_MAX_AGE_D", "42"))     # hard cutoff (~2 half-lives+)
PROFILE_MIN_HOURS = float(os.environ.get("CMON_PROFILE_MIN_HOURS", "24"))     # dense coverage needed
PROFILE_MIN_DAYS = int(os.environ.get("CMON_PROFILE_MIN_DAYS", "3"))          # days of token_log needed
PROFILE_MIN_BUCKETS = int(os.environ.get("CMON_PROFILE_MIN_BUCKETS", "8"))    # distinct hours needed
PROFILE_MIN_BUCKET_H = 0.5   # below this an hour bucket is "unobserved" (one noisy sample)
PROFILE_PSEUDO_H = 1.0       # shrinkage strength toward the local prior, in observed-hour units
PROFILE_FLOOR = 0.02         # no hour is ever exactly 0 intensity (kills divide-by-zero anchors)
PROFILE_ACTIVE_FRAC = 0.05   # a day-hour is "active" above this fraction of the mean intensity
QUIET_MULT = 0.05            # intensity multiplier inside declared quiet hours
# Forecast logging (see _log_forecasts / backtest): predictions are stored so they can be
# scored against reality later. Kept cheap — one write per interval, pruned by age.
FORECAST_EVERY_S = int(os.environ.get("CMON_FORECAST_EVERY_S", "900"))  # dedup between writes
FORECAST_HORIZONS = (1.0, 3.0, 6.0)  # fixed horizons, plus each window's own time-to-reset
FORECAST_MATCH_MIN = int(os.environ.get("CMON_FORECAST_MATCH_MIN", "20"))  # snapshot match window
FORECAST_KEEP_D = int(os.environ.get("CMON_FORECAST_KEEP_D", "60"))  # retention
# Tunable model parameters, with the values used until 'cmon tune' has evidence to change them.
# Persisted as one small JSON row in `meta` (a few hundred bytes) rather than a new table or
# file: they are three numbers, and they belong next to the history they were fitted on.
#   halflife_d — recency half-life of the learned shape, in days
#   harmonics  — 0 = 3-point circular smoothing; 2-3 = weighted Fourier fit of the daily cycle
#   blend      — how much of the profile to trust vs the flat rate (1 = all profile, 0 = linear)
#   robust     — damp rarely-used hours: est × occupancy^robust, occupancy = recency-weighted
#                share of days with real activity in that hour (0 = plain mean). A single 4am
#                burst night stops crediting every future 4am with burst-level intensity.
PARAM_DEFAULTS = {"halflife_d": 10.0, "harmonics": 0.0, "blend": 1.0, "robust": 0.5}
PARAM_GRID = {"halflife_d": [5.0, 10.0, 21.0], "harmonics": [0.0, 2.0, 3.0],
              "blend": [0.0, 0.5, 1.0], "robust": [0.0, 0.5, 1.0]}
TUNE_STEP = 24  # subsample the history when tuning: 27 candidates x a full replay is minutes
TUNE_MIN_GAIN = 0.05  # MAE points a candidate must win by before it's worth persisting
PARAM_MIN_SCORED = int(os.environ.get("CMON_TUNE_MIN_SCORED", "200"))  # per-band evidence needed
# How each projection source is described to the user, keyed by _project()'s third return value.
PROJ_NOTE = {"learned": " (weighted by your usage profile)",
             "quiet_hours": " (skipping your quiet hours)"}
PROJ_NOTE_SHORT = {"learned": " (profile-weighted)", "quiet_hours": " (quiet hours skipped)"}
PROJ_NOTE_WATCH = {"learned": "projection: profile-weighted",
                   "quiet_hours": "projection: skips quiet hours"}
# Claude Code OAuth — public values from login flow; used only for token renewal.
OAUTH_CLIENT_ID = os.environ.get("CMON_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
OAUTH_TOKEN_URL = os.environ.get("CMON_OAUTH_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token")


try:  # orjson speeds up log parsing ~2-3x; plain json is the fallback.
    import orjson
    _loads = orjson.loads
except ImportError:
    _loads = json.loads


class FetchError(Exception):
    """Failure to query the usage endpoint, with user-readable message."""


def _keyring():
    """keyring module, or None if absent (optional runtime dependency)."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _insecure_backend(kr) -> str | None:
    """Backend name if keyring would store the token in cleartext (the plaintext/null/fail
    fallback keyring silently picks on headless/CI), else None — so we never persist a token
    unencrypted while the user believes it sits in a secure vault (secperf F2)."""
    try:
        backend = kr.get_keyring()
    except Exception:
        return None  # can't introspect -> don't block the caller
    cls = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    if any(bad in cls for bad in ("plaintext", ".null.", ".fail.")):
        return getattr(backend, "name", cls)
    return None


def _claude_code_cred() -> dict | None:
    """Claude Code claudeAiOauth blob if logged in: {accessToken, refreshToken, expiresAt}.
    Cross-platform (file on Linux/Windows, Keychain on macOS)."""
    path = os.path.expanduser("~/.claude/.credentials.json")  # Linux, Windows
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)["claudeAiOauth"]
        except Exception:
            pass
    if sys.platform == "darwin":  # macOS stores in Keychain, not file
        try:
            blob = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, check=True).stdout
            return json.loads(blob)["claudeAiOauth"]
        except Exception:
            pass
    return None


def _session():
    """requests.Session with trust_env=False: ignores ambient HTTP(S)_PROXY / netrc / CA
    from the environment, so a stray .env can't route token-bearing calls through a MITM."""
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


def _anthropic_host(url: str) -> bool:
    """True only if url targets Anthropic. Guards the CMON_OAUTH_TOKEN_URL override so a
    stray .env can't redirect the refresh_token POST to an attacker endpoint (secperf F1)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return True
    print(f"cmon: refusing token refresh to non-Anthropic host {host!r} "
          "(CMON_OAUTH_TOKEN_URL). Unset it to use the default.", file=sys.stderr)
    return False


def _oauth_refresh(refresh_token: str) -> dict | None:
    """Exchange refresh_token for a new access_token. Best-effort; None if it fails.
    Only talks to Anthropic's OAuth endpoint (host-allowlisted) over a trust_env=False
    session, so a stray .env proxy/endpoint override can't exfiltrate the token."""
    import time
    if not _anthropic_host(OAUTH_TOKEN_URL):
        return None
    try:
        r = _session().post(OAUTH_TOKEN_URL, timeout=30, json={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID})
        if r.status_code != 200:
            return None
        d = r.json()
        if not d.get("access_token"):
            return None
        return {"accessToken": d["access_token"],
                "refreshToken": d.get("refresh_token", refresh_token),
                "expiresAt": int((time.time() + int(d.get("expires_in", 3600))) * 1000)}
    except Exception:
        return None


def _auto_load() -> dict | None:
    kr = _keyring()
    if not kr:
        return None
    try:
        raw = kr.get_password(SERVICE, AUTO_ACCOUNT)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _auto_save(blob: dict) -> None:
    kr = _keyring()
    if not kr or _insecure_backend(kr):
        return  # never persist the refresh chain in cleartext on an insecure backend (F2)
    try:
        kr.set_password(SERVICE, AUTO_ACCOUNT, json.dumps(blob))
    except Exception:
        pass


def _auto_token() -> tuple[str | None, str | None]:
    """(source, access_token) from self-managed chain: prefers cmon's own, then re-bootstraps
    from Claude Code. Renews via refresh_token when access expires. (None, None) if none.
    Falls back to Claude Code's credentials (fresh after any re-login) when cmon's chain can't
    renew, so a dead/rotated refresh_token never strands us on an expired access token."""
    import time
    now = time.time() * 1000
    auto = _auto_load()
    # 1. Unexpired access token already in cmon's chain (60s buffer).
    if auto and auto.get("accessToken") and now < (auto.get("expiresAt") or 0) - 60_000:
        return "cmon auto-refresh", auto["accessToken"]
    # 2. Renew cmon's chain via its own refresh_token.
    if auto and (rt := auto.get("refreshToken")) and (new := _oauth_refresh(rt)):
        _auto_save(new)
        return "cmon auto-refresh", new["accessToken"]
    # 3. cmon's chain is missing or its refresh_token is dead -> (re-)bootstrap from Claude Code.
    if cc := _claude_code_cred():
        if cc.get("accessToken") and now < (cc.get("expiresAt") or 0) - 60_000:
            _auto_save(cc)  # seed chain so the next run starts from a known-good blob
            return "Claude Code credentials", cc["accessToken"]
        if (rt := cc.get("refreshToken")) and (new := _oauth_refresh(rt)):
            _auto_save(new)
            return "cmon auto-refresh", new["accessToken"]
    # 4. Nothing renewable: hand back any stale token (fetch() force-refreshes on the 401).
    if auto and auto.get("accessToken"):
        return "cmon auto-refresh", auto["accessToken"]
    return None, None


def _force_refresh() -> str | None:
    """Force a refresh (used when API returns 401). Returns new access_token or None.
    Tries cmon's chain refresh_token first, then Claude Code's — so a dead/rotated chain
    token self-heals from Claude Code's fresh one, even if an old CLAUDE_OAUTH_TOKEN (env/.env)
    is shadowing everything."""
    seen: set[str] = set()
    for blob in (_auto_load(), _claude_code_cred()):
        rt = (blob or {}).get("refreshToken")
        if rt and rt not in seen:
            seen.add(rt)
            if new := _oauth_refresh(rt):
                _auto_save(new)
                return new["accessToken"]
    return None


def _resolve_token() -> tuple[str | None, str | None]:
    """(source, token) in order of precedence; (None, None) if none found."""
    if tok := os.environ.get("CLAUDE_OAUTH_TOKEN"):
        return "env CLAUDE_OAUTH_TOKEN", tok
    if kr := _keyring():
        try:
            if tok := kr.get_password(SERVICE, ACCOUNT):
                return f"OS vault ({kr.get_keyring().name})", tok
        except Exception:
            pass
    return _auto_token()


def get_token() -> str:
    _src, tok = _resolve_token()
    if not tok:
        sys.exit("No token. Run 'cmon token set' to save it securely, "
                 "set CLAUDE_OAUTH_TOKEN, or log in to Claude Code.")
    return tok


def _mask(tok: str) -> str:
    """Never print the full token: only prefix and suffix."""
    return f"{tok[:12]}…{tok[-4:]}" if len(tok) > 20 else "…"


def _safe(s, limit: int = 200) -> str:
    """Strip control/ANSI chars from untrusted log-derived strings (model/project/session
    ids) and cap length, before they reach the DB or terminal — blocks terminal-escape
    injection from a crafted transcript (secperf F4)."""
    return "".join(c for c in str(s) if c.isprintable())[:limit] or "?"


def token_set(_):
    kr = _keyring()
    if not kr:
        sys.exit("'keyring' library missing. Run 'uv sync' to install it.")
    # stdin isatty → hidden prompt; else read from pipe (e.g., echo $TOK | cmon token set).
    tok = (getpass.getpass("Paste OAuth token (hidden): ") if sys.stdin.isatty()
           else sys.stdin.readline()).strip()
    if not tok:
        sys.exit("Empty token — nothing saved.")
    if (bad := _insecure_backend(kr)) and os.environ.get("CMON_ALLOW_PLAINTEXT_KEYRING") != "1":
        sys.exit(f"Refusing to save: keyring backend '{bad}' stores secrets in cleartext.\n"
                 "Install a secure backend (macOS Keychain / gnome-keyring / Windows Credential\n"
                 "Manager), or use CLAUDE_OAUTH_TOKEN. Override with CMON_ALLOW_PLAINTEXT_KEYRING=1.")
    try:
        kr.set_password(SERVICE, ACCOUNT, tok)
    except Exception as e:
        sys.exit(f"Failed to access OS vault: {e}\n"
                 "On headless Linux install a backend (e.g., gnome-keyring) "
                 "or use CLAUDE_OAUTH_TOKEN.")
    print(f"✓ Token saved to OS vault ({kr.get_keyring().name}).")


def token_status(_):
    src, tok = _resolve_token()
    if not tok:
        print("No token available. Run 'cmon token set'.")
        return
    print(f"Source: {src}\nToken : {_mask(tok)}")
    if auto := _auto_load():  # chain renewed by cmon
        exp = auto.get("expiresAt")
        if exp:
            import time
            rem = (exp / 1000 - time.time()) / 3600
            print(f"Auto  : refreshed; expires in {rem:.1f}h" if rem > 0 else "Auto  : expired (renews on next use)")


def token_clear(_):
    kr = _keyring()
    if not kr:
        sys.exit("'keyring' library missing. Run 'uv sync' to install it.")
    n = 0
    for acct in (ACCOUNT, AUTO_ACCOUNT):
        try:
            kr.delete_password(SERVICE, acct)
            n += 1
        except Exception:
            pass
    print(f"Removed from OS vault ({n} entry/entries)." if n else "Nothing was saved in OS vault.")


# --- Config: persisted user preferences (~/.cmon/config.json) ---
CONFIG = os.path.expanduser("~/.cmon/config.json")
CONFIG_KEYS = {"timezone", "quiet_hours"}  # allowlist; extend here as new preferences are added


def _validate_timezone(value: str) -> str:
    """Raise ValueError with a user-facing message if `value` isn't a valid IANA tz name."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as e:
        raise ValueError(
            f"Unknown timezone {value!r}. Use an IANA name, e.g. 'America/Sao_Paulo', 'UTC'.") from e
    return value


def _validate_quiet_hours(value: str) -> str:
    """Normalize a quiet-hours spec ('2-7', '0-6,13', '23-2' wrapping midnight) or raise."""
    if not _parse_quiet_hours(value):
        raise ValueError(f"Invalid quiet_hours {value!r}. Use local hours like '2-7' or '0-6,13' "
                         "(ranges may wrap midnight, e.g. '23-2').")
    return value.replace(" ", "")


def _parse_quiet_hours(value: str | None) -> frozenset[int]:
    """Local hours declared idle -> {hours}, or empty frozenset if unset/unparseable.
    Accepts comma-separated single hours and inclusive ranges; a range whose start is
    greater than its end wraps midnight ('23-2' = 23, 0, 1, 2)."""
    if not value:
        return frozenset()
    hours: set[int] = set()
    for part in str(value).replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part[1:]:
                a_s, b_s = part.split("-", 1)
                a, b = int(a_s), int(b_s)
                if not (0 <= a <= 23 and 0 <= b <= 23):
                    return frozenset()
                hours |= {h % 24 for h in range(a, b + 1)} if a <= b else \
                         {h % 24 for h in range(a, b + 25)}
            else:
                h = int(part)
                if not 0 <= h <= 23:
                    return frozenset()
                hours.add(h)
        except ValueError:
            return frozenset()
    return frozenset(hours) if len(hours) < 24 else frozenset()  # all-quiet is meaningless


def _quiet_hours() -> frozenset[int]:
    """Effective quiet hours: CMON_QUIET_HOURS env -> config 'quiet_hours' -> none.
    Deterministic backstop for the projection: it applies even before enough history exists
    to learn a profile, so a projection never spends a night you don't work."""
    for src in (os.environ.get("CMON_QUIET_HOURS"), _read_config().get("quiet_hours")):
        if hours := _parse_quiet_hours(src):
            return hours
    return frozenset()


# key -> fn(value) -> normalized value | raises ValueError
CONFIG_VALIDATORS = {"timezone": _validate_timezone, "quiet_hours": _validate_quiet_hours}


def _read_config() -> dict:
    """Persisted user preferences, or {} if absent/unreadable/corrupt — degrades to defaults,
    same spirit as _read_cache."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_config(cfg: dict) -> None:
    """Persist user preferences to ~/.cmon/config.json. Unlike _write_cache this does NOT
    swallow errors — 'config set/clear' is the primary action, callers must surface failure."""
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def _resolve_tz():
    """Effective ZoneInfo for display, in precedence order: CMON_TZ env (quick override,
    doesn't persist) -> ~/.cmon/config.json {"timezone": ...} -> UTC. An invalid value at
    either source is skipped with a stderr warning, falling through."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    for source, name in (("env CMON_TZ", os.environ.get("CMON_TZ")),
                         ("config", _read_config().get("timezone"))):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            print(f"cmon: ignoring invalid timezone {name!r} from {source} "
                 "(run 'cmon config set timezone <IANA name>').", file=sys.stderr)
    return ZoneInfo("UTC")


def config_set(args):
    value = args.value
    if validator := CONFIG_VALIDATORS.get(args.key):
        try:
            value = validator(value)
        except ValueError as e:
            sys.exit(str(e))
    cfg = _read_config()
    cfg[args.key] = value
    try:
        _write_config(cfg)
    except Exception as e:
        sys.exit(f"Failed to save config: {e}")
    print(f"✓ {args.key} = {value}")


def config_get(args):
    val = _read_config().get(args.key)
    print(val if val is not None else f"{args.key} not set (default applies).")


def config_list(_args):
    cfg = _read_config()
    if not cfg:
        print("No preferences set. Run 'cmon config set <key> <value>'.")
        return
    w = max(len(k) for k in cfg)
    for k, v in cfg.items():
        print(f"{k:<{w}} = {v}")


def config_clear(args):
    cfg = _read_config()
    if args.all:
        try:
            _write_config({})
        except Exception as e:
            sys.exit(f"Failed to clear config: {e}")
        print("✓ All preferences cleared." if cfg else "Nothing was set.")
        return
    if not args.key:
        sys.exit("Specify a key to clear, or use --all.")
    if args.key not in cfg:
        print(f"{args.key} was not set.")
        return
    del cfg[args.key]
    try:
        _write_config(cfg)
    except Exception as e:
        sys.exit(f"Failed to save config: {e}")
    print(f"✓ {args.key} cleared.")


def _retry_after(r) -> float | None:
    """Seconds from a Retry-After header (429/503), clamped to [0, 60]; None if non-numeric.
    The clamp caps a malicious/garbage value so it can't hang the CLI (secperf F3)."""
    try:
        return min(max(float(r.headers.get("Retry-After") or ""), 0.0), 60.0)
    except ValueError:
        return None


def fetch(retries: int = RETRIES) -> dict:
    """Query usage endpoint with retry/backoff. Raises FetchError with user-readable message —
    401/403 fail immediately (no point retrying); 429 and 5xx and network errors retry
    with exponential backoff (respecting Retry-After)."""
    import time

    import requests
    last, override = "?", None
    s = _session()  # trust_env=False: don't leak the Bearer token through an ambient proxy
    for attempt in range(retries):
        # override = newly refreshed token after 401; overrides even old CLAUDE_OAUTH_TOKEN.
        headers = {"Authorization": f"Bearer {override or get_token()}",
                   "anthropic-beta": "oauth-2025-04-20", "User-Agent": UA}
        try:
            r = s.get(URL, timeout=30, headers=headers)
            if r.status_code == 401:
                if override is None and (new := _force_refresh()):
                    override = new
                    continue
                raise FetchError("401 — invalid or expired token. Open Claude Code to "
                                 "refresh, set CLAUDE_OAUTH_TOKEN, or run 'cmon token set'.")
            if r.status_code == 403:
                raise FetchError("403 — blocked (Cloudflare/User-Agent) or no access. "
                                 "The private endpoint may have changed.")
            if r.status_code == 429 or r.status_code >= 500:
                last, wait = f"HTTP {r.status_code}", _retry_after(r) or 2 ** attempt
            else:
                r.raise_for_status()
                return r.json()
        except requests.RequestException as e:
            last, wait = f"network: {e}", 2 ** attempt
        if attempt < retries - 1:
            time.sleep(wait)
    raise FetchError(f"Failed to query {URL} after {retries} attempts ({last}).")


def limits(data: dict) -> list[tuple]:
    """Normalize limits[] -> (key, label, percent, resets_at, is_active). key is stable for delta."""
    out = []
    for lim in data.get("limits", []):
        kind = lim["kind"]
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        key = f"{kind}:{model}" if model else kind
        label = LABELS.get(kind) or (f"{model} only" if model else kind)
        out.append((key, label, float(lim["percent"]), lim.get("resets_at"), lim.get("is_active")))
    return out


def db(create: bool = True):
    """DuckDB connection. create=False returns None if database doesn't yet exist (doesn't create file)."""
    if not create and not os.path.exists(DB):
        return None
    import duckdb
    if create:
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    con = duckdb.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS snapshots("
                "ts TIMESTAMPTZ, key TEXT, label TEXT, percent DOUBLE, "
                "resets_at TIMESTAMPTZ, is_active BOOL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT, ts TEXT)")
    # Forecast log: what we predicted, and (once target_ts passes) what actually happened.
    # Closes the loop — 'cmon backtest' scores MAE/bias per horizon, and the tuner reads the
    # same rows to pick parameters. Small by design: one row per (poll, key, horizon), deduped
    # to FORECAST_EVERY_S and pruned past FORECAST_KEEP_D.
    con.execute("CREATE TABLE IF NOT EXISTS forecasts("
                "ts TIMESTAMPTZ, key TEXT, horizon_h DOUBLE, target_ts TIMESTAMPTZ, "
                "pred_pct DOUBLE, pct0 DOUBLE, rate DOUBLE, model TEXT, "
                "actual_pct DOUBLE, resolved_ts TIMESTAMPTZ)")
    _ensure_token_tables(con)  # read paths (burn line, hourly profile) query token_terminal
    # What counts against the quota cmon reads: the terminal's account (~/.claude.json). The
    # Claude Desktop app can be signed into another account yet writes its Claude Code
    # sessions to the same ~/.claude/projects (entrypoint 'claude-desktop'); those count only
    # when Desktop's own session index ties them to the terminal account. Unattributable
    # Desktop sessions are left out — over-reporting someone else's usage is the worse error.
    con.execute("CREATE VIEW IF NOT EXISTS token_terminal AS SELECT * FROM token_log "
                "WHERE entrypoint <> 'claude-desktop' OR session IN (SELECT session FROM session_account "
                "WHERE account = (SELECT value FROM meta WHERE key = 'terminal_account'))")
    return con


def _meta_get(con, key: str) -> tuple[str, float] | None:
    """(value, age_seconds) for a learned-state key, or None. age lets callers ignore stale state."""
    if con is None:
        return None
    try:
        r = con.execute("SELECT value, ts FROM meta WHERE key=?", [key]).fetchone()
    except Exception:
        return None
    if not r:
        return None
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(r[1])).total_seconds() if r[1] else 1e9
    except (ValueError, TypeError):
        age = 1e9
    return r[0], age


def _meta_set(con, key: str, value: str) -> None:
    if con is None:
        return
    try:
        con.execute("INSERT INTO meta VALUES (?,?,?) ON CONFLICT (key) DO UPDATE SET "
                    "value=excluded.value, ts=excluded.ts",
                    [key, value, datetime.now(UTC).isoformat()])
    except Exception:
        pass


def deltas(con):
    """delta = percent - previous snapshot for same window; delta<0 = reset (not consumption).
    Plot-only: returns a pandas DataFrame (via .df()), so it needs the `plot` extra. Core
    code paths use SQL aggregation directly (see _summary) and never call this."""
    df = con.execute(
        "SELECT ts, key, label, percent, percent - lag(percent) "
        "OVER (PARTITION BY key ORDER BY ts) AS delta "
        "FROM snapshots ORDER BY ts").df()
    if df.empty:
        sys.exit("No data — run 'cmon collect' a few times first.")
    return df


def bar(pct: float, width: int = 20) -> str:
    fill = int(min(pct, 100) / 100 * width)
    return "█" * fill + "·" * (width - fill)


def fmt_eta(iso: str | None) -> str:
    if not iso:
        return "-"
    secs = (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()
    if secs < 0:
        return "expired"
    h, m = divmod(int(secs // 60), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def now(args):
    rows, _ts = _snapshot()  # fetch + best-effort persist so every 'now' also feeds history
    print("Current usage:")
    for _k, lbl, pct, reset, _act in rows:
        print(f"  {lbl:16} {bar(pct)} {pct:4.0f}%   resets in {fmt_eta(reset):>9}")

    if getattr(args, "advice", False):  # 'now --advice' = usage + full pacing tips (was 'cmon tips')
        print("\nPacing — spend ~100% of weekly without blocking the 5h window:\n")
        _advice(rows, _read_db(), no_ai=getattr(args, "no_ai", False))
        return

    sess = next((r for r in rows if r[0] == "session"), None)
    if not sess:
        return
    _k, _lbl, pct, reset, _a = sess
    print(f"\n5h window: {pct:.0f}% used — expires in {fmt_eta(reset)}.")

    if not reset:
        return
    con = _read_db()  # lazy: only import/open DuckDB once we know the projection is needed (F10)
    if con is None:
        return
    end = datetime.fromisoformat(reset)
    rate = _rate(con, "session")  # unified EWMA rate: same source as watch/advice/alerts
    if rate is None:
        print("Rate: no measurable consumption in this window.")
        return
    now_utc = datetime.now(UTC)
    rem_h = (end - now_utc).total_seconds() / 3600
    proj, to100, src = _project(con, pct, rate, rem_h, now_utc, _resolve_tz(), "session")
    note = PROJ_NOTE.get(src, "")
    print(f"Rate: {rate:.1f}%/h → projection at reset: {min(proj, 100):.0f}%{note}.")
    if to100 is not None:
        print(f"⚠ At current pace you'll hit 100% in ~{to100:.1f}h, before reset.")

    for m in _alerts(rows, con):
        if not m.startswith("Current session"):  # 5h already covered above
            print(f"⚠ {m}")


def _recent(con, ts) -> bool:
    """True if a snapshot was saved within DEDUP_SECS of ts (dedup guard, shared by collectors)."""
    return bool(con.execute("SELECT count(*) FROM snapshots WHERE ts > ?",
                            [ts - timedelta(seconds=DEDUP_SECS)]).fetchone()[0])


def _read_db():
    """Open the DB for reading, or None if it's absent or busy — never raises. DuckDB is
    single-writer, so a running 'watch'/'collect' holds the lock; read paths degrade instead
    of crashing."""
    try:
        return db(create=False)
    except Exception:
        return None


def _snapshot(con=None):
    """Fetch usage, best-effort persist it (deduped) and refresh the cache; return (rows, ts).
    Single path for the read-mostly commands (now/watch/wait) so no fetch is wasted — they
    all feed the same history rate/projection/trends read. Persist is best-effort: with no `con`
    a short-lived write connection is opened and closed (so long-running watch/wait never hold
    the single-writer lock), and any DB-busy error is swallowed — the fetch still returns."""
    rows = limits(fetch())
    ts = datetime.now(UTC)
    own = con is None
    try:
        c = db() if own else con
        if not _recent(c, ts):
            c.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                          [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
        if own:
            c.close()
    except Exception:
        pass  # DB busy/unavailable → best-effort; cache still refreshes below
    _write_cache(rows, ts)
    return rows, ts


def collect(args):
    con = db()  # dedicated collector: hold the write lock and surface errors (cron logs them)
    ts = datetime.now(UTC)
    if not getattr(args, "force", False) and _recent(con, ts):
        print(f"Snapshot <{DEDUP_SECS}s ago — skipped (use --force to save anyway).")
        return
    rows = limits(fetch())
    con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    [[ts, k, lbl, pct, reset, act] for k, lbl, pct, reset, act in rows])
    _write_cache(rows, ts)
    _log_forecasts(con, rows, _resolve_tz(), ts)  # record predictions; scored by 'cmon backtest'
    _reconcile_forecasts(con)                     # and settle the ones whose horizon just passed
    print(f"{ts:%Y-%m-%d %H:%M} collected:")
    for _k, lbl, pct, reset, _a in rows:
        print(f"  {lbl:16} {pct:4.0f}%  reset {reset[:16] if reset else '-'}")
    if getattr(args, "alert", False):
        for m in _alerts(rows, con):  # rate-based: at current pace hits 100% before reset
            print(f"⚠ {m}", file=sys.stderr)
            _notify("cmon — Claude limit", m)
        _fire_lead_alert(rows)  # time-based: 5h window resets in <= CMON_ALERT_LEAD min (+ hook)


def import_db(args):
    """Merge history from other cmon databases (e.g. the old per-directory usage.duckdb) into
    DB. Sources are attached read-only and never modified. Snapshots within DEDUP_SECS of
    one already stored for the same key are skipped, so overlapping collectors don't double
    the series; token rows are re-deduplicated on the next scan (see scan_logs)."""
    con = db()
    target = os.path.realpath(DB)
    for i, path in enumerate(args.paths):
        src = os.path.realpath(os.path.expanduser(path))
        if src == target:
            print(f"skip {path}: it is the target database")
            continue
        if not os.path.exists(src):
            sys.exit(f"cmon: {path}: not found")
        alias = f"src{i}"
        con.execute(f"ATTACH '{src.replace(chr(39), chr(39) * 2)}' AS {alias} (READ_ONLY)")
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = ?", [alias]).fetchall()}
        before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("snapshots", "forecasts")}
        if "snapshots" in tables:
            con.execute(
                f"INSERT INTO snapshots SELECT s.ts, s.key, s.label, s.percent, s.resets_at, s.is_active "
                f"FROM {alias}.snapshots s WHERE NOT EXISTS (SELECT 1 FROM snapshots t WHERE t.key = s.key "
                f"AND t.ts BETWEEN s.ts - to_seconds(?) AND s.ts + to_seconds(?))",
                [DEDUP_SECS, DEDUP_SECS])
        if "forecasts" in tables:
            con.execute(
                f"INSERT INTO forecasts SELECT f.* FROM {alias}.forecasts f WHERE NOT EXISTS ("
                f"SELECT 1 FROM forecasts t WHERE t.key = f.key AND t.ts = f.ts AND t.horizon_h = f.horizon_h)")
        tokens = 0
        if "token_log" in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{alias}.token_log')").fetchall()]
            if "entrypoint" in cols:
                _ensure_token_tables(con)
                n0 = con.execute("SELECT count(*) FROM token_log").fetchone()[0]
                con.execute(f"INSERT INTO token_log SELECT uuid, ts, model, entrypoint, project, session, "
                            f"in_tok, out_tok, cache_read, cache_create FROM {alias}.token_log "
                            f"ON CONFLICT DO NOTHING")
                tokens = con.execute("SELECT count(*) FROM token_log").fetchone()[0] - n0
        con.execute(f"DETACH {alias}")
        added = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] - before[t] for t in before}
        print(f"{path}: +{added['snapshots']} snapshots, +{added['forecasts']} forecasts, "
              f"+{tokens} token rows")
    _meta_set(con, TOKEN_KEY_META, "")  # imported rows may carry legacy keys: re-dedupe on next scan
    print(f"merged into {DB}")


def _parse_since(s: str | None):
    """'24h', '7d' or ISO date/time -> datetime UTC. None if empty or 'all' (everything)."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("all", "*"):
        return None
    now_utc = datetime.now(UTC)
    if s.endswith("h"):
        return now_utc - timedelta(hours=float(s[:-1]))
    if s.endswith("d"):
        return now_utc - timedelta(days=float(s[:-1]))
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _summary(con, since):
    """Per-label aggregate over the optional --since window: snapshots, peak %, and total
    consumed % (sum of positive deltas). The headline table shown by 'trends'. Returns a
    list of dict rows sorted by label. delta is computed over the full history (lag per key)
    and only then filtered to the window, so a snapshot at the window edge still gets a delta."""
    where = "WHERE ts >= ?" if since is not None else ""
    params = [since] if since is not None else []
    rows = con.execute(
        "WITH d AS (SELECT label, percent, ts, "
        "  percent - lag(percent) OVER (PARTITION BY key ORDER BY ts) AS delta FROM snapshots) "
        f"SELECT label, count(*), round(max(percent)), "
        f"  round(sum(CASE WHEN delta > 0 THEN delta ELSE 0 END)) "
        f"FROM d {where} GROUP BY label ORDER BY label", params).fetchall()
    if not rows:
        if since is not None:
            since_local = since.astimezone(_resolve_tz())
            sys.exit(f"No data since {since_local:%Y-%m-%d %H:%M %Z}.")
        sys.exit("No data — run 'cmon collect' a few times first.")
    return [{"label": lbl, "snapshots": int(n), "peak_pct": int(pk),
             "consumed_pct": int(cons or 0)} for lbl, n, pk, cons in rows]


def _open_file(path):
    """Open file in OS default app (best-effort; silent if it fails)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass


def _time_formatter(tz):
    """Axis time labels as '16h' / '16h40' (drop ':00'); date ('Jul 05') at day boundaries so
    multi-day panels keep a reference. tz-aware via the axis' own tz (else labels shift by UTC offset)."""
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter

    def fmt(x, _pos=None):
        dt = mdates.num2date(x, tz=tz)
        if dt.hour == 0 and dt.minute == 0:
            return dt.strftime("%b %d")
        return f"{dt:%H}h" if dt.minute == 0 else f"{dt:%H}h{dt:%M}"
    return FuncFormatter(fmt)


def _pace_ax(ax, con, key: str, title: str, now_utc) -> None:
    """Actual cumulative % of the CURRENT cycle vs. the even-pace line to 100% at reset.
    Above the line = burning faster than even → hits 100% before reset; below = buffer left.
    Faceted per window because session (5h) and weekly (7d) horizons share no time axis.
    Reading differs by goal: weekly → aim for the line (full use); session → stay below it
    (crossing 100% early blocks you)."""
    segs = _cycles(con, key)
    if not segs or len(segs[-1].ts) < 1:
        ax.text(0.5, 0.5, "no data yet", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title)
        return
    seg = segs[-1]  # current cycle (after last reset)
    ax.plot(seg.ts, seg.percent, marker="o", label="actual")
    rrow = con.execute("SELECT resets_at FROM snapshots WHERE key=? AND resets_at IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", [key]).fetchone()
    if rrow and rrow[0] is not None:
        reset = rrow[0]  # tz-aware datetime (DuckDB TIMESTAMPTZ, needs pytz)
        x0, y0 = seg.ts[0], float(seg.percent[0])
        ax.plot([x0, reset], [y0, 100], ls="--", color="gray", label="even pace → 100% at reset")
        ax.axvline(reset, color="red", ls=":", lw=1)
        if x0 <= now_utc <= reset:
            ax.axvline(now_utc, color="green", ls=":", lw=1, label="now")
    ax.set_ylim(0, 105)
    ax.set_title(title)
    ax.set_xlabel("")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(_time_formatter(seg.ts[0].tzinfo))
    ax.tick_params(axis="x", rotation=30)


def _burn_ax(ax, con, since) -> None:
    """Stacked API-equivalent cost/day by model from token_log — the richest table (where the
    tokens actually go). You pay a subscription, not this; it's for relative comparison."""
    scan_logs(con, since=since, quiet=True)
    where = "WHERE ts >= ?" if since else ""
    df = con.execute(
        f"SELECT CAST(ts AS DATE) d, model, sum(in_tok) i, sum(out_tok) o, "
        f"sum(cache_read) r, sum(cache_create) c FROM token_terminal {where} GROUP BY d, model",
        [since] if since else []).df()
    if df.empty:
        ax.text(0.5, 0.5, "no Claude Code log data", ha="center", va="center", transform=ax.transAxes)
        ax.set_title("Burn — cost/day by model")
        return
    df["cost"] = [(r.i * _price(r.model)[0] + r.o * _price(r.model)[1]
                   + r.r * _price(r.model)[2] + r.c * _price(r.model)[3]) / 1e6
                  for r in df.itertuples()]
    df["m"] = df.model.map(_short_model)
    import pandas as pd
    piv = df.pivot_table(index="d", columns="m", values="cost", aggfunc="sum", fill_value=0)
    piv.plot(kind="bar", stacked=True, ax=ax)
    ax.set_title("Burn — API-equivalent cost/day by model (US$)")
    ax.set_ylabel("US$")
    ax.set_xlabel("")
    # pandas labels bars with the raw datetime index ('2026-06-09 00:00:00'): reformat to 'Jun 09'.
    ax.set_xticklabels(pd.to_datetime(piv.index).strftime("%b %d"), rotation=45, ha="right", fontsize=8)
    ax.legend(fontsize=8, title="")


def _ensure_plot_deps():
    """Import the plotting stack, installing the optional `plot` extra on first use if it's
    missing. Tries pip in the running interpreter, then `uv pip` (uv-managed envs often ship
    without pip), and only falls back to manual instructions if both fail."""
    try:
        import matplotlib.pyplot  # noqa: F401
        import seaborn  # noqa: F401
        return
    except ImportError:
        pass
    pkgs = ["matplotlib>=3.8", "seaborn>=0.13", "pandas>=2.0"]
    print("plot needs matplotlib/seaborn/pandas — installing them now…", file=sys.stderr)
    import importlib
    for cmd in ([sys.executable, "-m", "pip", "install", *pkgs],
                ["uv", "pip", "install", "--python", sys.executable, *pkgs]):
        try:
            subprocess.run(cmd, check=True)
            importlib.invalidate_caches()  # make the freshly-installed packages importable now
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    sys.exit("Auto-install failed. Install the plot extra manually:\n"
             "  pip install 'cmon-cli[plot]'       (via PyPI)\n"
             "  uv tool install 'cmon-cli[plot]'   (if installed with uv tool)\n"
             "  uv sync --extra plot               (in the repo)")


def plot(args):
    _ensure_plot_deps()
    import matplotlib.pyplot as plt
    import seaborn as sns
    con = db()
    df = deltas(con)
    now_utc = datetime.now(UTC)
    since = _parse_since(getattr(args, "since", None) or "30d")
    sns.set_theme(style="whitegrid")
    fig, axd = plt.subplot_mosaic(
        [["traj", "traj"], ["pace_s", "pace_w"], ["burn", "burn"]], figsize=(14, 15))
    sns.lineplot(df, x="ts", y="percent", hue="label", marker="o", ax=axd["traj"])
    axd["traj"].set_title("Usage (%) over time")
    axd["traj"].set_xlabel("")
    axd["traj"].set_ylim(0, 105)
    axd["traj"].xaxis.set_major_formatter(_time_formatter(df.ts.dt.tz))
    _pace_ax(axd["pace_s"], con, "session", "Pace — 5h window (stay below line = don't block)", now_utc)
    _pace_ax(axd["pace_w"], con, "weekly_all", "Pace — Weekly all models (aim for line = full use)", now_utc)
    _burn_ax(axd["burn"], con, since)
    fig.tight_layout()
    out = args.out or f"usage_{datetime.now():%y%m%d_%H%M%S}.png"
    fig.savefig(out, dpi=150)
    print(f"{out} saved")
    _open_file(out)


def _rate_half_life(key) -> float:
    """EWMA half-life (hours) used to measure a window's current rate. Short for the 5h window
    (its pace changes within a session), long for weekly (a day of noise shouldn't swing it).
    _project needs the same number to know what span its input rate is an average *over*."""
    return 1.0 if key == "session" else 12.0


_rate_cache: dict[tuple, float | None] = {}  # (key, as_of) -> rate; replay/tune only


def _rate(con, key, as_of=None) -> float | None:
    """Weighted %/h in the current window (segment since the last reset). Exponentially
    weighted least-squares slope: recent snapshots dominate (half-life ~1h for the 5h window,
    ~12h weekly), so the projection tracks your current pace instead of a flat cycle average.
    None if there's no net rise to project. Self-adapts to 5h/7d/etc. by cutting at each reset.

    as_of pretends it is that moment and ignores every later snapshot — the replay backtest
    needs it, since scoring a past prediction against a rate computed from data that only
    arrived afterwards would leak the future into the model being judged."""
    if con is None:
        return None
    # Past rates never change, and 'cmon tune' re-scores the same thousands of moments once per
    # candidate — so memoize them. Only for as_of: the live rate must stay fresh.
    if as_of is not None and (ck := (key, as_of)) in _rate_cache:
        return _rate_cache[ck]
    # epoch_us keeps this off the TIMESTAMPTZ->datetime path (we only need time *deltas*).
    rows = con.execute("SELECT epoch_us(ts), percent FROM snapshots WHERE key=? "
                       "AND (? IS NULL OR ts <= ?) ORDER BY ts",
                       [key, as_of, as_of]).fetchall()
    if as_of is not None:
        _rate_cache[(key, as_of)] = None  # replaced below unless we bail out early
    if len(rows) < 2:
        return None
    last_drop = 0  # index of the last reset (percent dropped vs. previous sample)
    for i in range(1, len(rows)):
        if rows[i][1] - rows[i - 1][1] < 0:
            last_drop = i
    seg = rows[last_drop:]  # only the segment after the last reset
    if len(seg) < 2:
        return None
    import math
    # t = hours relative to the newest sample (<=0); exp(t/half_life) decays weight into the past.
    t_last = seg[-1][0]
    t = [(us - t_last) / 1e6 / 3600.0 for us, _ in seg]
    y = [float(p) for _, p in seg]
    half_life = _rate_half_life(key)
    w = [math.exp(ti / half_life) for ti in t]
    sw = sum(w)
    tm = sum(wi * ti for wi, ti in zip(w, t, strict=True)) / sw
    denom = sum(wi * (ti - tm) ** 2 for wi, ti in zip(w, t, strict=True))
    if denom <= 0:  # all samples effectively at one instant
        return None
    ym = sum(wi * yi for wi, yi in zip(w, y, strict=True)) / sw
    slope = sum(wi * (ti - tm) * (yi - ym) for wi, ti, yi in zip(w, t, y, strict=True)) / denom  # %/h, EWMA
    out = float(slope) if slope > 0 else None
    if as_of is not None:
        _rate_cache[(key, as_of)] = out
    return out


_profile_cache: dict[str, tuple[float, tuple | None]] = {}  # tz_key -> (expires_epoch, profile)
_params_cache: dict | None = None


def _params(con=None) -> dict:
    """Active model parameters: PARAM_DEFAULTS overridden by whatever 'cmon tune' last wrote.
    Cached per process — every watch poll reads these."""
    global _params_cache
    if _params_cache is None:
        p = dict(PARAM_DEFAULTS)
        if r := _meta_get(con, "model_params"):
            try:
                p.update({k: float(v) for k, v in json.loads(r[0]).items() if k in PARAM_DEFAULTS})
            except (ValueError, TypeError):
                pass
        _params_cache = p
    return _params_cache


def _set_params(con, p: dict | None) -> None:
    """Persist tuned parameters (or, with None, only reset the process cache — the tuner walks
    a grid in-process and each candidate needs the learned shape recomputed)."""
    global _params_cache
    _params_cache = p if p is None else {**PARAM_DEFAULTS, **p}
    _profile_cache.clear()
    if p is not None and con is not None:
        _meta_set(con, "model_params", json.dumps({k: round(v, 4) for k, v in p.items()}))


def _solve(a) -> list[float] | None:
    """Gaussian elimination with partial pivoting on an augmented n x (n+1) matrix. None if
    singular. Small by construction (n <= 7), so no numpy — the core stays dependency-free."""
    n = len(a)
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(a[r][i]))
        if abs(a[piv][i]) < 1e-12:
            return None
        a[i], a[piv] = a[piv], a[i]
        for r in range(i + 1, n):
            f = a[r][i] / a[i][i]
            for c in range(i, n + 1):
                a[r][c] -= f * a[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (a[i][n] - sum(a[i][c] * x[c] for c in range(i + 1, n))) / a[i][i]
    return x


def _fourier(weights, y, k: int) -> list[float] | None:
    """Weighted least-squares fit of the 24 hourly values y to a mean plus k harmonics of the
    daily cycle, evaluated back at each hour. Cyclic by construction — hour 23 flows into hour 0,
    which a 3-point smoother can only approximate — and 2k+1 parameters for 24 buckets, so it
    regresses the noise instead of interpolating it, with better-observed hours weighing more.

    Interpolation (a polynomial through all 24 points) would be the wrong tool: it doesn't close
    the cycle, oscillates (Runge) on noisy buckets, and can swing negative. Clamped at 0 anyway,
    since a truncated series can undershoot."""
    import math
    n = 2 * k + 1

    def basis(h):
        b = [1.0]
        for j in range(1, k + 1):
            ang = 2 * math.pi * j * h / 24
            b += [math.cos(ang), math.sin(ang)]
        return b

    rows = [basis(h) for h in range(24)]
    a = [[0.0] * (n + 1) for _ in range(n)]
    for h in range(24):
        w = weights[h]
        if w <= 0:
            continue
        for r in range(n):
            for c in range(n):
                a[r][c] += w * rows[h][r] * rows[h][c]
            a[r][n] += w * rows[h][r] * y[h]
    beta = _solve(a)
    if beta is None:
        return None
    return [max(sum(b * v for b, v in zip(rows[h], beta, strict=True)), 0.0) for h in range(24)]


def _day_weights(con, tz, now_utc, halflife_d):
    """{local date -> recency weight} for every day from the first logged request (capped at
    PROFILE_MAX_AGE_D) through today, plus the exposed hours of today. A day's weight decays
    with a PROFILE_HALFLIFE_D half-life, so the learned shape follows your current routine
    and old habits fade instead of dropping off a cliff when a fixed window slides."""
    cutoff = now_utc - timedelta(days=PROFILE_MAX_AGE_D)
    first = con.execute("SELECT min(ts) FROM token_terminal WHERE ts > ?", [cutoff]).fetchone()[0]
    if first is None:
        return {}, 0
    now_local = now_utc.astimezone(tz)
    day = max(first.astimezone(tz), cutoff.astimezone(tz)).date()
    out = {}
    while day <= now_local.date():
        age_d = (now_local.date() - day).days
        out[day] = 0.5 ** (age_d / halflife_d)
        day += timedelta(days=1)
    return out, now_local.hour + 1  # hours of today already exposed


def _hour_buckets_tokens(con, tz, now_utc, halflife_d):
    """(hrs, pts, whrs, wpts, act) per local hour/weekday from token_log, or None when it can't
    support a shape. This is the good source: every assistant request carries its own precise
    timestamp, so an idle 4am is an *observed zero* rather than an unknown. Snapshot percents
    can't do that — they only move in whole points and a gap between two of them says nothing
    about when inside it the work happened.

    Weight mixes fresh tokens with cache reads at 1/10 (a cached-heavy hour is real work but
    costs proportionally less quota). 'Exposure' (hrs) is one weighted hour per day covered,
    which is what makes an hour with no requests count as genuinely dead. act[h] is the weighted
    exposure of days with *real* activity in that hour (above PROFILE_ACTIVE_FRAC of the mean,
    so a stray heartbeat request doesn't count) — act[h]/hrs[h] is the occupancy the `robust`
    parameter raises to damp rarely-used hours."""
    days, today_hours = _day_weights(con, tz, now_utc, halflife_d)
    if len(days) < PROFILE_MIN_DAYS:
        return None
    hrs, pts = [0.0] * 24, [0.0] * 24
    whrs, wpts = [0.0] * 7, [0.0] * 7
    act = [0.0] * 24
    for day, w in days.items():
        exposed = range(today_hours) if day == max(days) else range(24)
        for h in exposed:
            hrs[h] += w
        whrs[day.weekday()] += w * len(exposed)
    # Aggregate in SQL (a few hundred rows out) instead of walking every request in Python:
    # token_log holds 100k+ rows and this runs on every watch poll after the cache expires.
    rows = con.execute(
        "SELECT (timezone(?, ts))::DATE, hour(timezone(?, ts)), "
        "sum(in_tok + out_tok + coalesce(cache_read, 0) / 10.0) FROM token_terminal "
        "WHERE ts > ? AND ts <= ? AND in_tok IS NOT NULL GROUP BY 1, 2",
        [str(tz), str(tz), now_utc - timedelta(days=PROFILE_MAX_AGE_D), now_utc]).fetchall()
    for day, hour, tok in rows:
        w = days.get(day)
        if w is None or not tok:
            continue
        pts[hour] += w * float(tok)
        wpts[day.weekday()] += w * float(tok)
    total_h = sum(hrs)
    mean = sum(pts) / total_h if total_h > 0 else 0.0
    for day, hour, tok in rows:
        w = days.get(day)
        if w is not None and tok and float(tok) >= PROFILE_ACTIVE_FRAC * mean:
            act[hour] += w
    return (hrs, pts, whrs, wpts, act) if sum(pts) > 0 else None


def _hour_buckets(con, tz, now_utc, halflife_d):
    """Recency-weighted (observed_hours, consumed_pct) per local hour and per weekday, from
    *dense* consecutive weekly_all snapshot pairs. Fallback for when token_log is empty (no
    Claude Code transcripts scanned yet). Two rules make the histogram honest:

    - only pairs closer than PROFILE_PAIR_MAX_H count. A wider gap cannot say *when* inside
      it the usage happened; spreading its delta evenly (the old behaviour) credited idle
      nights with consumption and then dominated the totals, flattening the whole profile.
    - each pair's contribution decays with a PROFILE_HALFLIFE_D half-life, so the shape
      follows your current routine and old habits fade instead of dropping off a cliff.

    weekly_all is the one key whose percent climbs through day *and* night between rare
    resets, so its deltas can cover the full daily cycle — the 5h key loses every night to
    a reset drop. Returns (hrs, pts, whrs, wpts)."""
    hrs, pts = [0.0] * 24, [0.0] * 24
    whrs, wpts = [0.0] * 7, [0.0] * 7
    rows = con.execute("SELECT epoch_us(ts), percent FROM snapshots WHERE key='weekly_all' "
                       "AND ts > ? AND ts <= ? ORDER BY ts",
                       [now_utc - timedelta(days=PROFILE_MAX_AGE_D), now_utc]).fetchall()
    now_s = now_utc.timestamp()
    for (us0, p0), (us1, p1) in zip(rows, rows[1:], strict=False):
        dt_h = (us1 - us0) / 3.6e9
        d = p1 - p0
        if dt_h <= 0 or dt_h > PROFILE_PAIR_MAX_H or d < 0:  # d<0 = reset inside the gap
            continue
        rate = d / dt_h
        w = 0.5 ** (((now_s - us1 / 1e6) / 86400.0) / halflife_d)
        t = datetime.fromtimestamp(us0 / 1e6, tz)
        end = datetime.fromtimestamp(us1 / 1e6, tz)
        while t < end:  # a dense pair spans 1-2 buckets, so this loop is short
            nxt = min((t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), end)
            seg = w * (nxt - t).total_seconds() / 3600.0
            hrs[t.hour] += seg
            pts[t.hour] += rate * seg
            whrs[t.weekday()] += seg
            wpts[t.weekday()] += rate * seg
            t = nxt
    return hrs, pts, whrs, wpts


def _local_prior(raw, mean):
    """Fill unobserved hours (raw[h] is None) by circular interpolation between the nearest
    observed hours on each side — a night with no data inherits its neighbours, not the daily
    mean. The mean is a terrible prior for a dead hour: it is dominated by working hours and
    was exactly what kept 3am looking busy. All-None (no data at all) -> mean everywhere."""
    obs = [h for h in range(24) if raw[h] is not None]
    if not obs:
        return [mean] * 24
    out = []
    for h in range(24):
        if raw[h] is not None:
            out.append(raw[h])
            continue
        prev = max(obs, key=lambda o: -((h - o) % 24))   # nearest observed hour going back
        nxt = min(obs, key=lambda o: (o - h) % 24)       # nearest observed hour going forward
        back, fwd = (h - prev) % 24, (nxt - h) % 24
        span = back + fwd
        out.append(raw[prev] if span == 0 else (raw[prev] * fwd + raw[nxt] * back) / span)
    return out


def _hour_profile(con, tz, as_of=None):
    """(hour_rate[24], dow_mult[7], mean, source) — relative usage intensity by local hour and
    weekday, or None when neither history nor config can say anything. as_of restricts learning
    to data available at that moment (replay backtest; see _rate).

    Learned from token_log when available (observed zeros: an hour with no requests is dead,
    not unknown) and from dense snapshot pairs otherwise, where an unobserved hour has to
    borrow from its neighbours via _local_prior instead. Below the coverage gate the learned
    shape is dropped; if quiet_hours is configured a synthetic profile (flat awake, QUIET_MULT
    asleep) is used instead, enough to stop the projection from spending the night. Cached
    ~15 min: watch re-renders every poll."""
    quiet = _quiet_hours()
    synthetic = ([QUIET_MULT if h in quiet else 1.0 for h in range(24)],
                 [1.0] * 7, 1.0, "quiet_hours") if quiet else None
    if con is None:
        return synthetic
    now_utc = as_of or datetime.now(UTC)
    now_s = datetime.now(UTC).timestamp()
    par = _params(con)
    halflife, harmonics = par["halflife_d"], int(par["harmonics"])
    # A replay caches per (tz, as_of hour): it walks thousands of past moments, and relearning
    # the shape for each one would dominate its runtime.
    ck = f"{tz}|{as_of:%Y%m%d%H}" if as_of else str(tz)
    cached = _profile_cache.get(ck)
    if cached and cached[0] > now_s:
        return cached[1]
    try:
        buckets = _hour_buckets_tokens(con, tz, now_utc, halflife)
    except Exception:  # token_log absent (older database) -> snapshot fallback
        buckets = None
    observed_zeros = buckets is not None
    act = None
    if buckets is None:
        buckets = _hour_buckets(con, tz, now_utc, halflife)
    else:
        *buckets, act = buckets
    hrs, pts, whrs, wpts = buckets
    total_h = sum(hrs)
    mean = sum(pts) / total_h if total_h > 0 else 0.0
    covered = sum(1 for h in hrs if h >= PROFILE_MIN_BUCKET_H)
    prof = synthetic
    if total_h >= PROFILE_MIN_HOURS and covered >= PROFILE_MIN_BUCKETS and mean > 0:
        raw = [pts[h] / hrs[h] if hrs[h] >= PROFILE_MIN_BUCKET_H else None for h in range(24)]
        if observed_zeros:
            est = [v if v is not None else 0.0 for v in raw]
            # The mean over-credits rare bursts: one heavy 4am among idle nights leaves every
            # future 4am carrying burst/n intensity. Raising occupancy (how *often* the hour is
            # active, not how hard) damps rarely-used hours toward dead while leaving the shape
            # of habitual hours to the mean. robust=0 turns this off.
            if act is not None and par["robust"] > 0:
                est = [e * (act[h] / hrs[h]) ** par["robust"] if hrs[h] > 0 else e
                       for h, e in enumerate(est)]
        else:  # interval-censored: an unobserved hour inherits its neighbours, not the mean
            prior = _local_prior(raw, mean)
            est = [(pts[h] + PROFILE_PSEUDO_H * prior[h]) / (hrs[h] + PROFILE_PSEUDO_H)
                   if raw[h] is not None else prior[h] for h in range(24)]
        fitted = _fourier(hrs, est, harmonics) if harmonics >= 1 else None
        smooth = fitted or [0.25 * est[(h - 1) % 24] + 0.5 * est[h] + 0.25 * est[(h + 1) % 24]
                            for h in range(24)]
        smooth = [max(v, PROFILE_FLOOR * mean) for v in smooth]
        if quiet:  # an explicit statement beats an inference from sparse data
            smooth = [v * QUIET_MULT if h in quiet else v for h, v in enumerate(smooth)]
        dow = [min(max(((wpts[d] + PROFILE_PSEUDO_H * mean) / (whrs[d] + PROFILE_PSEUDO_H)) / mean,
                       0.25), 4.0) if whrs[d] >= 2 else 1.0 for d in range(7)]
        # The returned mean must describe the *returned* shape: occupancy damping shrinks the
        # profile as a whole, and _project floors its anchor at 0.25×mean — an undamped mean
        # there would cap k and quietly deflate every projection.
        mean_out = sum(smooth[h] * hrs[h] for h in range(24)) / total_h
        prof = (smooth, dow, mean_out if mean_out > 0 else mean, "learned")
    _profile_cache[ck] = (now_s + 900, prof)
    return prof


def _project(con, pct, rate, rem_h, now_utc, tz, key=None, replay=False) -> tuple[float, float | None, str]:
    """(projection at reset, hours to hit 100% or None if it doesn't before reset, source).
    source is "" (linear), "learned" (hourly/weekday profile) or "quiet_hours" (declared
    idle hours only) — callers use it for the note they print, and it is truthy iff weighted.
    With a profile, integrates expected intensity over the remaining window instead of the flat
    rate×hours extrapolation, which overshoots into idle periods (nights) and undershoots into
    busy ones. Linear without a profile.

    The anchor k converts the measured rate into profile units, and *what* it divides by matters:
    the input rate is an EWMA over roughly the last _rate_half_life(key) hours, not the intensity
    of this instant. Dividing by intensity(now) therefore mis-scaled every weekly projection —
    with a 12h half-life the measured rate already averages evening peaks with the night, so
    anchoring it to a peak-hour intensity made the model quietly pessimistic (replay: bias -9.1
    at horizons past 6h, worse than plain linear). Dividing by the intensity averaged under the
    same exponential kernel is the consistent choice. Floored at 25% of the mean so a burst
    during a historically dead hour can't inflate every future busy hour by that ratio.

    replay=True treats now_utc as the present, so a past prediction is rebuilt from the data
    that existed back then."""
    prof = _hour_profile(con, tz, as_of=now_utc if replay else None)
    if prof is None:
        eta = (100 - pct) / rate if pct < 100 else None
        return pct + rate * rem_h, (eta if eta is not None and eta < rem_h else None), ""
    hour_rate, dow_mult, mean, src = prof

    def intensity(t_local):
        return hour_rate[t_local.hour] * dow_mult[t_local.weekday()]

    now_local = now_utc.astimezone(tz)
    hl = _rate_half_life(key)
    span, dt = 3 * hl, 0.25   # 3 half-lives carry ~90% of the kernel's weight
    kern = [(0.5 ** (a / hl), intensity(now_local - timedelta(hours=a)))
            for a in [i * dt + dt / 2 for i in range(int(span / dt))]]
    past = sum(w * v for w, v in kern) / sum(w for w, _ in kern)
    k = rate / max(past, 0.25 * mean)
    # blend < 1 keeps part of the flat rate. Short horizons have almost no shape to exploit, so
    # the tuner can pull the profile's weight down where the backtest says it doesn't pay off.
    lam = _params(con)["blend"]
    if lam <= 0:
        eta = (100 - pct) / rate if pct < 100 else None
        return pct + rate * rem_h, (eta if eta is not None and eta < rem_h else None), ""
    acc, eta, t = 0.0, None, 0.0
    while t < rem_h:
        dt = min(0.25, rem_h - t)
        shaped = k * intensity(now_local + timedelta(hours=t + dt / 2))
        acc += (lam * shaped + (1 - lam) * rate) * dt
        t += dt
        if eta is None and pct < 100 and pct + acc >= 100:
            eta = t
    return pct + acc, eta, src


def _log_forecasts(con, rows, tz, now_utc=None) -> None:
    """Record what we predict right now so it can be scored later (see _reconcile_forecasts).
    Both the active model *and* the plain linear baseline are stored for each horizon, which
    is the only way to tell whether the profile actually helps rather than just looks smarter.
    Best-effort and deduped to one write per FORECAST_EVERY_S: watch polls every ~75s and this
    is bookkeeping, not the user's request."""
    if con is None:
        return
    now_utc = now_utc or datetime.now(UTC)
    try:
        last = con.execute("SELECT max(ts) FROM forecasts").fetchone()[0]
        if last and (now_utc - last).total_seconds() < FORECAST_EVERY_S:
            return
        out = []
        for key, _lbl, pct, reset, _act in rows:
            if not reset or pct >= 100:
                continue
            rate = _rate(con, key)
            if not rate:
                continue
            rem_h = (datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
            if rem_h <= 0:
                continue
            for h in [x for x in FORECAST_HORIZONS if x < rem_h] + [rem_h]:
                pred, _eta, src = _project(con, pct, rate, h, now_utc, tz, key)
                target = now_utc + timedelta(hours=h)
                out.append([now_utc, key, h, target, pred, pct, rate, src or "linear"])
                if src:  # baseline for comparison; skipped when the model *is* linear
                    out.append([now_utc, key, h, target, pct + rate * h, pct, rate, "linear"])
        if out:
            con.executemany("INSERT INTO forecasts (ts, key, horizon_h, target_ts, pred_pct, "
                            "pct0, rate, model) VALUES (?,?,?,?,?,?,?,?)", out)
    except Exception:
        pass  # never let bookkeeping break a collect/watch cycle


def _reconcile_forecasts(con) -> int:
    """Fill actual_pct for forecasts whose target time has passed, from the nearest snapshot of
    the same window within FORECAST_MATCH_MIN minutes. Returns how many were resolved.

    Pairs whose window reset between prediction and target are dropped, not scored: percent
    falling below pct0 means the quota reset, so the 'error' would be the reset, not the model.
    Unmatched predictions older than a day (no snapshot near their target — machine asleep)
    and rows past FORECAST_KEEP_D are pruned so the table stays small."""
    if con is None:
        return 0
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _rec AS SELECT fts, fkey, fh, sts, percent FROM ("
            "  SELECT f.ts AS fts, f.key AS fkey, f.horizon_h AS fh, s.ts AS sts, s.percent, "
            "         row_number() OVER (PARTITION BY f.ts, f.key, f.horizon_h "
            "           ORDER BY abs(date_diff('second', s.ts, f.target_ts))) AS rn "
            "  FROM forecasts f JOIN snapshots s ON s.key = f.key "
            "    AND s.ts BETWEEN f.target_ts - to_minutes(?) AND f.target_ts + to_minutes(?) "
            "  WHERE f.actual_pct IS NULL AND f.target_ts <= now()) WHERE rn = 1",
            [FORECAST_MATCH_MIN, FORECAST_MATCH_MIN])
        con.execute("UPDATE forecasts SET actual_pct = _rec.percent, resolved_ts = _rec.sts "
                    "FROM _rec WHERE forecasts.ts = _rec.fts AND forecasts.key = _rec.fkey "
                    "AND forecasts.horizon_h = _rec.fh")
        n = con.execute("SELECT count(*) FROM _rec").fetchone()[0]
        con.execute("DELETE FROM forecasts WHERE actual_pct IS NOT NULL AND actual_pct < pct0 - 0.5")
        con.execute("DELETE FROM forecasts WHERE actual_pct IS NULL AND target_ts < now() - to_hours(24)")
        con.execute("DELETE FROM forecasts WHERE ts < now() - to_days(?)", [FORECAST_KEEP_D])
        return int(n)
    except Exception:
        return 0


def _backtest_rows(con, since):
    """(model, band, n, mae, bias) per model and horizon band, from resolved forecasts.
    pred_pct is clamped to 100 for scoring: reality saturates there, so an uncapped 130%
    prediction would be charged for 30 points of error it could never have avoided."""
    return con.execute(
        "SELECT model, CASE WHEN horizon_h <= 1 THEN '<=1h' "
        "                   WHEN horizon_h <= 6 THEN '1-6h' ELSE '>6h' END AS band, "
        "       count(*), avg(abs(least(pred_pct, 100) - actual_pct)), "
        "       avg(least(pred_pct, 100) - actual_pct) "
        "FROM forecasts WHERE actual_pct IS NOT NULL AND ts > ? "
        "GROUP BY 1, 2 ORDER BY 1, min(horizon_h)", [since]).fetchall()


def _replay(con, tz, since, keys=("session", "weekly_all"), step=8, models=("active", "linear"),
            until=None):
    """Rebuild past predictions from the snapshots we already have and score them, without
    waiting weeks for live forecasts to mature. For each sampled snapshot it recomputes the
    rate and the profile *as of that moment* (nothing later is visible), projects each horizon,
    and compares against the actual percent at the target time — skipping pairs whose window
    reset in between, since the drop isn't a forecasting error.

    Returns (model, band, n, mae, bias) rows shaped like _backtest_rows. step subsamples the
    snapshots (they arrive every ~20min; every 8th is ~2.7h apart, plenty for scoring)."""
    out = {}
    for key in keys:
        rows = con.execute("SELECT ts, percent, resets_at FROM snapshots WHERE key=? AND ts > ? "
                           "AND resets_at IS NOT NULL ORDER BY ts", [key, since]).fetchall()
        if len(rows) < 4:
            continue
        stamps = [ts for ts, _p, _r in rows]
        tol = timedelta(minutes=FORECAST_MATCH_MIN)
        # `until` bounds the *predictions*, not the outcomes: a forecast made just before the
        # cutoff is still scored against the snapshot that resolves it, as it would be live.
        stop = bisect.bisect_right(stamps, until) if until else len(rows)
        for i in range(0, stop, step):
            ts, pct, reset = rows[i]
            rem_h = (reset - ts).total_seconds() / 3600
            rate = _rate(con, key, as_of=ts)
            if not rate or rem_h <= 0 or pct >= 100:
                continue
            for h in [x for x in FORECAST_HORIZONS if x < rem_h] + [rem_h]:
                target = ts + timedelta(hours=h)
                # first snapshot within the match window of the target; None -> unscoreable.
                # bisect, not a scan: the tuner replays this history once per candidate.
                j = bisect.bisect_left(stamps, target - tol, i + 1)
                act = (stamps[j], rows[j][1]) if j < len(rows) and stamps[j] <= target + tol else None
                if act is None or act[1] < pct - 0.5:  # reset between prediction and target
                    continue
                pred, _eta, src = _project(con, pct, rate, h, ts, tz, key, replay=True)
                band = "<=1h" if h <= 1 else ("1-6h" if h <= 6 else ">6h")
                for model, value in (("active", pred), ("linear", pct + rate * h)):
                    if model not in models:
                        continue
                    err = min(value, 100.0) - act[1]
                    agg = out.setdefault((model if model == "linear" else (src or "linear"), band),
                                         [0, 0.0, 0.0])
                    agg[0] += 1
                    agg[1] += abs(err)
                    agg[2] += err
    order = {"<=1h": 0, "1-6h": 1, ">6h": 2}
    return [(m, b, n, s_abs / n, s_err / n)
            for (m, b), (n, s_abs, s_err) in sorted(out.items(), key=lambda kv: (kv[0][0], order[kv[0][1]]))
            if n]


def backtest(args):
    """Score past projections against what actually happened. bias > 0 = overestimating."""
    con = _read_db()
    if con is None:
        sys.exit("No database yet — run 'cmon collect' a few times first.")
    if getattr(args, "replay", False):
        since = _parse_since(getattr(args, "since", None)) or datetime(1970, 1, 1, tzinfo=UTC)
        rows = _replay(con, _resolve_tz(), since)
        _print_backtest(rows, getattr(args, "json", False), header="Replayed over existing history")
        return
    resolved = _reconcile_forecasts(con)
    since = _parse_since(getattr(args, "since", None)) or datetime(1970, 1, 1, tzinfo=UTC)
    rows = _backtest_rows(con, since)
    if not rows and not getattr(args, "json", False):
        pend = con.execute("SELECT count(*) FROM forecasts WHERE actual_pct IS NULL").fetchone()[0]
        print(f"No scored forecasts yet ({pend} awaiting their target time). Keep "
              "'cmon collect'/'watch' running, or use --replay to score the history you "
              "already have.")
        return
    _print_backtest(rows, getattr(args, "json", False),
                    header=f"Live forecasts (resolved {resolved} new)")


def _score(con, tz, since, until=None, step=12) -> tuple[int, float] | None:
    """(n, MAE) of the active parameters over a replay slice, or None if nothing is scoreable."""
    rows = [r for r in _replay(con, tz, since, step=step, models=("active",), until=until)]
    n = sum(r[2] for r in rows)
    return (n, sum(r[2] * r[3] for r in rows) / n) if n else None


def tune(args):
    """Grid-search the model parameters against the recorded history and persist the winner.

    Fitted on the older part of the history and scored on the newer part — picking the
    parameters that look best on the very data used to choose them is how you end up with a
    model that only predicts the past. Refuses to write anything until PARAM_MIN_SCORED
    predictions are scoreable, because a grid search over a few dozen points fits noise."""
    con = _read_db()
    if con is None:
        sys.exit("No database yet — run 'cmon collect' a few times first.")
    if getattr(args, "show", False):
        cur, stored = _params(con), _meta_get(con, "model_params")
        print("Active model parameters:")
        for k in sorted(PARAM_DEFAULTS):
            tag = "" if stored else "  (default)"
            print(f"  {k:<11} {cur[k]:g}{tag}")
        if stored:
            print(f"\nTuned {stored[1] / 86400:.1f} days ago. 'cmon tune' re-fits, "
                  "'cmon tune --reset' restores defaults.")
        return
    if getattr(args, "reset", False):
        _set_params(con, dict(PARAM_DEFAULTS))
        _meta_set(con, "model_params", json.dumps(PARAM_DEFAULTS))
        print("✓ Model parameters restored to defaults.")
        return
    tz = _resolve_tz()
    active = dict(_params(con))
    span = con.execute("SELECT min(ts), max(ts) FROM snapshots WHERE key='session'").fetchone()
    if not span or not span[0]:
        sys.exit("No snapshots to tune on yet.")
    first, last = span
    split = first + (last - first) * 0.6  # 60% fit / 40% validate, in time order
    baseline = _score(con, tz, split, step=TUNE_STEP)
    if baseline is None or baseline[0] < PARAM_MIN_SCORED:
        have = baseline[0] if baseline else 0
        sys.exit(f"Not enough scoreable history to tune ({have} of {PARAM_MIN_SCORED} needed). "
                 "Keep collecting — 'cmon backtest --replay' works meanwhile.")
    keys = sorted(PARAM_GRID)
    combos = [dict(zip(keys, vals, strict=True))
              for vals in itertools.product(*(PARAM_GRID[k] for k in keys))]
    print(f"Fitting {len(combos)} candidates on {first:%Y-%m-%d} → {split:%Y-%m-%d}, "
          f"validating on {split:%Y-%m-%d} → {last:%Y-%m-%d}…")
    fitted = []
    for i, c in enumerate(combos, 1):
        _set_params(None, c)
        s = _score(con, tz, first, until=split, step=TUNE_STEP)
        if s:
            fitted.append((s[1], c))
        print(f"\r  {i}/{len(combos)}", end="", flush=True)
    print()
    if not fitted:
        sys.exit("Nothing scoreable in the fitting window.")
    fitted.sort(key=lambda x: x[0])
    best_fit = fitted[0][1]
    _set_params(None, best_fit)
    val = _score(con, tz, split, step=TUNE_STEP)
    _set_params(None, active)  # the incumbent is whatever is active now, not the defaults
    cur = _score(con, tz, split, step=TUNE_STEP)
    pstr = " ".join(f"{k}={v:g}" for k, v in best_fit.items())
    print(f"\n  {'params':<34} {'fit MAE':>8} {'val MAE':>8}")
    if cur:
        print(f"  {'current':<34} {'':>8} {cur[1]:>8.2f}")
    if val:
        print(f"  {pstr:<34} {fitted[0][0]:>8.2f} {val[1]:>8.2f}")
    # TUNE_MIN_GAIN keeps a coin-flip difference from being written as an improvement.
    if not val or not cur or val[1] >= cur[1] - TUNE_MIN_GAIN:
        _set_params(None, active)  # restore in-process only; nothing persisted, nothing lost
        print("\nNo candidate beat the current parameters on unseen data — keeping them "
              "unchanged. Expected until there's more history.")
        return
    _set_params(con, best_fit)
    print(f"\n✓ Saved: {pstr}  (validation MAE {cur[1]:.2f} → {val[1]:.2f}). "
          "'cmon tune --reset' undoes this.")


def _print_backtest(rows, as_json: bool, header: str) -> None:
    if as_json:
        print(json.dumps([{"model": m, "horizon": b, "n": int(n), "mae": round(mae, 2),
                           "bias": round(bias, 2)} for m, b, n, mae, bias in rows], indent=2))
        return
    if not rows:
        print("Nothing scoreable in that range.")
        return
    print(f"{header}:\n")
    print(f"  {'model':<12} {'horizon':<8} {'n':>6} {'MAE':>7} {'bias':>7}")
    for m, band, n, mae, bias in rows:
        print(f"  {m:<12} {band:<8} {n:>6} {mae:>7.2f} {bias:>+7.2f}")
    print("\nMAE = mean absolute error in percentage points; bias > 0 = projecting too high.")


def _alerts(rows, con, tz=None) -> list[str]:
    """Alert when, at the projected pace, a window hits 100% before reset.
    Needs history (via _rate); without database/rate produces nothing. watch passes its
    pre-resolved tz (a _resolve_tz here could print into the Live TUI)."""
    now_utc = datetime.now(UTC)
    tz = tz or _resolve_tz()
    msgs = []
    for key, lbl, pct, reset, _a in rows:
        if not reset or pct >= 100:
            continue
        rate = _rate(con, key)
        if not rate:
            continue
        rem_h = (datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
        if rem_h <= 0:
            continue
        _proj, eta, _w = _project(con, pct, rate, rem_h, now_utc, tz, key)
        if eta is not None:
            msgs.append(f"{lbl}: 100% in ~{eta:.1f}h (~{rem_h - eta:.1f}h before reset)")
    return msgs


def _notify(title: str, body: str) -> None:
    """Native notification best-effort (osascript on macOS, notify-send on Linux).
    Silent if tool doesn't exist — never crashes the command."""
    import shutil
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(body)} "
                            f"with title {json.dumps(title)}"],
                           capture_output=True, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], capture_output=True, timeout=10)
    except Exception:
        pass


def _run_hook(msg: str) -> None:
    """Run the user's CMON_HOOK command on an alert (best-effort), with the alert text in the
    CMON_ALERT_MSG env var. Shell command, so it can be anything. Never raises — a broken hook
    must not break collection."""
    hook = os.environ.get("CMON_HOOK")
    if not hook:
        return
    try:
        subprocess.run(hook, shell=True, timeout=30,
                       env={**os.environ, "CMON_ALERT_MSG": msg}, capture_output=True)
    except Exception:
        pass


ALERT_STATE = os.path.expanduser("~/.cmon/alerts.json")  # dedup markers: alert kind -> last marker


def _alert_fired(key: str, marker) -> bool:
    """Best-effort dedup: True if alert `key` was already fired for `marker` (e.g. a resets_at)."""
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            return json.load(f).get(key) == marker
    except Exception:
        return False


def _mark_alert(key: str, marker) -> None:
    try:
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        d = {}
        try:
            with open(ALERT_STATE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            pass
        d[key] = marker
        with open(ALERT_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def _lead_alerts(rows):
    """Time-based lead alerts for every window in LEAD_MIN_BY_KEY (5h session + weekly). Yields
    (key, reset_dt, message) for each window within its own lead of resetting. Purely time-based
    (uses resets_at), independent of consumption rate."""
    by_key = {r[0]: r for r in rows}
    for key, lead_min in LEAD_MIN_BY_KEY.items():
        row = by_key.get(key)
        if not row or not row[3]:
            continue
        reset = datetime.fromisoformat(row[3]) if isinstance(row[3], str) else row[3]
        rem_min = (reset - datetime.now(UTC)).total_seconds() / 60
        if 0 < rem_min <= lead_min:
            lbl = LEAD_LABEL_BY_KEY.get(key, row[1])
            yield key, reset, f"{lbl} resets in ~{fmt_eta(row[3])} (used {row[2]:.0f}%)."


def _fire_lead_alert(rows) -> None:
    """Fire the time-based lead alert per window (5h + weekly), once per cycle each: stderr +
    native desktop notification + CMON_HOOK. Deduped per window on its own reset bucketed to the
    nearest minute — the API's resets_at jitters ~1s between calls, so an exact-string marker
    would re-fire every collect."""
    for key, reset, msg in _lead_alerts(rows):
        marker = round(reset.timestamp() / 60)  # nearest-minute bucket, robust to ~1s API jitter
        if _alert_fired(f"lead_{key}", marker):
            continue
        print(f"⚠ {msg}", file=sys.stderr)
        _notify(f"cmon — {LEAD_LABEL_BY_KEY.get(key, key)}", msg)
        _run_hook(msg)
        _mark_alert(f"lead_{key}", marker)


def _ai_tip(summary: str) -> str | None:
    """Pass numbers to Claude (sonnet, cheap) and return 3 tips. None if unavailable."""
    import shutil
    if not shutil.which("claude"):
        return None
    prompt = (
        "You optimize Claude plan usage. Current state:\n" + summary +
        "\n\nUser goal: get close to 100% of WEEKLY limit at reset — "
        "without running out early and without hitting 5h window (which blocks usage).\n"
        "Give max 3 one-line, actionable tips, prioritizing: pacing "
        "(speed up if buffer remains / slow down if you'll run out), model swap (Haiku "
        "cheap for simple tasks, Sonnet for code, Opus only for hard) and timing "
        "(peak 8h–14h ET on weekdays drains 5h faster). No preamble, no markdown. "
        "Use ONLY numbers above — don't invent missing rates or projections. "
        "Respond in English."
    )
    try:
        # --append-system-prompt forces English: 'claude -p' otherwise inherits the user's
        # CLAUDE.md, whose language directive would win over an in-prompt request.
        r = subprocess.run(
            ["claude", "-p", prompt, "--model", "sonnet", "--append-system-prompt",
             "Always respond in English, ignoring any global or project instruction "
             "to reply in another language."],
            capture_output=True, text=True, timeout=120)
        return r.stdout.strip() or None
    except Exception:
        return None


def _window_tips(lbl: str, pct: float, reset: str | None, rate: float | None, now_utc, con=None,
                 tz=None, key=None):
    """Deterministic pacing block for a window. Returns (printed_lines, summary_line).
    With con+tz the projection/eta use the hourly usage profile (same as watch/now)."""
    if not reset:
        return [f"{lbl}: {pct:.0f}% used."], f"- {lbl}: {pct:.0f}% used"
    rem_h = (datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
    if rem_h <= 0:
        return [f"{lbl}: {pct:.0f}% used, resetting now."], f"- {lbl}: {pct:.0f}%, reset imminent"
    tgt = (100 - pct) / rem_h  # %/h to hit exactly 100 at reset
    out = [f"{lbl}: {pct:.0f}% used · resets in {fmt_eta(reset)} · target to clear buffer {tgt:.2f}%/h"]
    summ = f"- {lbl}: {pct:.0f}% used, resets in {fmt_eta(reset)}, target {tgt:.2f}%/h"
    if rate is None:
        out.append("  (no rate yet — run 'cmon collect' more times)")
        return out, summ
    proj, eta, src = _project(con, pct, rate, rem_h, now_utc, tz or _resolve_tz(), key)
    note = PROJ_NOTE_SHORT.get(src, "")
    summ += f", rate {rate:.2f}%/h, projection {min(proj, 999):.0f}%{note}"
    out.append(f"  current rate {rate:.2f}%/h → projection at reset ~{min(proj, 999):.0f}%{note}")
    if proj < 97:
        gap = tgt - rate
        extra = f" (~+{gap:.2f}%/h)" if gap > 0 else ""
        out.append(f"  ↑ upside: ~{100 - proj:.0f}% left on table — can intensify{extra} "
                   "or use stronger model")
    elif proj > 103 and eta is not None:
        out.append(f"  ⚠ won't make it: hits 100% in ~{eta:.0f}h ({rem_h - eta:.0f}h before reset) — "
                   f"slow to ~{tgt:.2f}%/h or switch to cheaper model")
    else:
        out.append("  ✓ on track to hit close to 100% at reset")
    return out, summ


def _advice(rows, con, no_ai: bool = False):
    """Pacing advice for 'now --advice': per-window target %/h + projection + upside/shortfall,
    the real last-5h model mix (grounds the model-swap tip) and — unless no_ai — 3 one-line
    tips from Claude Sonnet. Skips the mix if the DB is busy."""
    now_utc = datetime.now(UTC)
    tz = _resolve_tz()
    summaries = []
    for key, want in (("weekly_all", "Weekly (All models)"), ("session", "5h window")):
        row = next((r for r in rows if r[0] == key), None)
        if not row:
            continue
        _k, _lbl, pct, reset, _a = row
        block, summ = _window_tips(want, pct, reset, _rate(con, key), now_utc, con, tz, key)
        print("\n".join(block) + "\n")
        summaries.append(summ)

    # scoped models (e.g., Fable/Opus) go only in summary so AI can suggest swap
    for key, lbl, pct, reset, _a in rows:
        if key not in ("weekly_all", "session"):
            summaries.append(f"- {lbl}: {pct:.0f}% used, resets in {fmt_eta(reset)}")

    # real model mix in last 5h (from logs) — grounds model swap advice; skipped if DB busy
    if con is not None:
        win = _session_window_start(rows, now_utc)
        scan_logs(con, since=win, quiet=True)
        bs = _burn_summary(_burn_rows(con, win))
        if bs:
            tok, cost, mix = bs
            line = f"Model mix (this 5h window): {tok / 1e6:.2f}M tok · US$ {cost:.2f} · {mix}"
            print(line + "\n")
            summaries.append("- " + line)

    if no_ai:
        return
    tip = _ai_tip("\n".join(summaries))
    if tip:
        print("Tips (Claude Sonnet):\n" + tip)
    else:
        print("(AI tip unavailable — 'claude' not found in PATH. Use --no-ai to silence.)")


def _eta_short(iso: str | None) -> str:
    """Compact fmt_eta for statusline: '3h 31m' -> '3h31m'."""
    e = fmt_eta(iso)
    return e.replace(" ", "") if e not in ("-", "expired") else e


CACHE = os.path.expanduser("~/.cmon/status.json")  # fixed: statusline runs from any cwd


def _write_cache(rows, ts) -> None:
    """Save latest snapshot to lightweight cache for 'status' to read without network/DuckDB."""
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": ts.isoformat(), "rows": [list(r) for r in rows]}, f)
    except Exception:
        pass


def _read_cache():
    """(rows, age_s) from JSON cache, or None if absent/unreadable."""
    if not os.path.exists(CACHE):
        return None
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        ts = datetime.fromisoformat(d["ts"])
        return [tuple(r) for r in d["rows"]], (datetime.now(UTC) - ts).total_seconds()
    except Exception:
        return None


def _rows_from_db():
    """(rows, age_s) from latest snapshot in database, or None if none."""
    con = db(create=False)
    if con is None:
        return None
    res = con.execute("SELECT key,label,percent,resets_at,is_active,ts FROM snapshots "
                      "WHERE ts=(SELECT max(ts) FROM snapshots)").fetchall()
    if not res:
        return None
    rows = [(key, label, float(percent),
             resets_at.isoformat() if resets_at is not None else None,
             bool(is_active))
            for key, label, percent, resets_at, is_active, _ts in res]
    return rows, (datetime.now(UTC) - res[0][5]).total_seconds()


def _reset_expired(rows) -> bool:
    """True if the session window's cached reset already passed — a dead cycle whose % is
    stale and misleading (shows a used-up window that has since rolled over)."""
    sess = next((r for r in rows if r[0] == "session"), None)
    if not sess or not sess[3]:
        return False
    try:
        return datetime.fromisoformat(sess[3]) <= datetime.now(UTC)
    except (ValueError, TypeError):
        return False


def _status_rows(live: bool):
    """(rows, age_s) for 'status': local cache by default (fast, no network) fed by
    'collect'; live forces API. A cache whose session window already reset is
    auto-refreshed from the API (and re-cached) so callers never see a dead 'reset
    expired' cycle; on network failure it falls back to the stale rows. None = offline
    with nothing cached. age_s is 0 for rows just fetched."""
    if live:
        try:
            rows = limits(fetch())
        except FetchError:
            return None
        _write_cache(rows, datetime.now(UTC))  # warm the cache so plain 'status' stays fresh
        return rows, 0.0
    got = _read_cache() or _rows_from_db()
    if got and not _reset_expired(got[0]):
        return got
    try:
        rows = limits(fetch(retries=1))  # no/stale cache: refresh and re-cache
    except FetchError:
        return got  # offline: keep stale rows (or None); staleness shown via age
    _write_cache(rows, datetime.now(UTC))
    return rows, 0.0


def _status_json(got) -> dict:
    """Stable machine contract for 'status --json' (menu bar, scripts). Versioned and
    grouped under 'providers' so more providers can be added without breaking readers."""
    if got is None:
        return {"version": 1, "offline": True, "observed_at": None, "age_s": None, "providers": {}}
    rows, age = got
    observed = datetime.now(UTC) - timedelta(seconds=age)
    windows = [{"key": key, "label": label, "percent": percent, "resets_at": resets_at,
                "active": bool(active)} for key, label, percent, resets_at, active in rows]
    return {"version": 1, "offline": False, "observed_at": observed.isoformat(),
            "age_s": round(age), "providers": {"claude": {"windows": windows}}}


def status(args):
    """Single line for statusline/tmux/prompt (or --json for the menu bar/scripts). Reads
    the local cache by default (~sub-20ms, no network); --live forces API."""
    got = _status_rows(args.live)
    if args.json:
        print(json.dumps(_status_json(got)))
        return
    if got is None:
        print("cmon offline")
        return
    rows, age = got
    parts = []
    for key, short in (("session", "5h"), ("weekly_all", "wk")):
        r = next((x for x in rows if x[0] == key), None)
        if r:
            parts.append(f"{short} {r[2]:.0f}%")
    sess = next((x for x in rows if x[0] == "session"), None)
    if sess and sess[3]:
        parts.append(f"reset {_eta_short(sess[3])}")
    if age > 1800:  # stale data: signal without noise
        parts.append(f"{age / 60:.0f}m ago")
    print(" · ".join(parts))


def wait(args):
    """Block until window resets (default) or hits --at N%, then notify.
    Ctrl-C cancels. Poll every --interval s; in reset mode sleep until resets_at."""
    import time
    key = args.window

    def get():
        rows, _ts = _snapshot()  # con-less: best-effort short-lived persist, no long-held lock
        return next((r for r in rows if r[0] == key), None)

    try:
        row = get()
        if not row:
            sys.exit(f"Window '{key}' not found on endpoint.")
        _k, lbl, pct0, reset, _a = row

        if args.at is not None:
            print(f"Waiting for {lbl} to hit {args.at}% (current {pct0:.0f}%)… Ctrl-C quits.")
            while True:
                r = get()
                if r and r[2] >= args.at:
                    msg = f"{lbl} hit {r[2]:.0f}% (threshold {args.at}%)."
                    print(msg)
                    _notify("cmon — threshold reached", msg)
                    return
                time.sleep(args.interval)

        if not reset:
            sys.exit(f"{lbl} has no scheduled reset — nothing to wait for.")
        target = datetime.fromisoformat(reset)
        print(f"Waiting for {lbl} to reset (~{fmt_eta(reset)}, {pct0:.0f}% used now)… Ctrl-C quits.")
        while True:
            now_utc = datetime.now(UTC)
            if now_utc >= target:
                r = get()
                if r is None or r[2] < pct0 or r[3] != reset:
                    msg = f"{lbl} reset — you can resume."
                    print(msg)
                    _notify("cmon — window released", msg)
                    return
                time.sleep(min(args.interval, 30))  # reset not yet reflected; re-check
            else:
                time.sleep(min((target - now_utc).total_seconds(), args.interval))
    except KeyboardInterrupt:
        print("\ncanceled.")


def _cycles(con, key: str) -> list:
    """Segment snapshots for a window into cycles, cutting at each reset
    (percent drop). Returns a list of Cycle(ts, percent) named tuples."""
    if con is None:
        return []
    rows = con.execute("SELECT ts, percent FROM snapshots WHERE key=? ORDER BY ts", [key]).fetchall()
    if not rows:
        return []
    cuts = [i for i in range(1, len(rows)) if rows[i][1] - rows[i - 1][1] < 0]  # reset boundaries
    bounds = [0, *cuts, len(rows)]
    segs = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if a < b:
            chunk = rows[a:b]
            segs.append(Cycle([r[0] for r in chunk], [float(r[1]) for r in chunk]))
    return segs


def trends(args):
    """Consumption history: a per-label summary (snapshots/peak/total consumed over --since,
    --json for scripts) followed by the reset-aware per-cycle breakdown — peak per cycle,
    delta vs. previous, and an anomaly alert if the current cycle exceeds the average."""
    con = db(create=False)
    if con is None:
        sys.exit("No history — run 'cmon collect' a few times first.")
    g = _summary(con, _parse_since(getattr(args, "since", None)))
    if getattr(args, "json", False):
        print(json.dumps(g, ensure_ascii=False))
        return
    cols = [("label", "label", "<"), ("snapshots", "snapshots", ">"),
            ("peak_pct", "peak_pct", ">"), ("consumed_pct", "consumed_pct", ">")]
    widths = {k: max(len(h), *(len(str(r[k])) for r in g)) for k, h, _ in cols}
    print("  ".join(f"{h:{a}{widths[k]}}" for k, h, a in cols))
    for r in g:
        print("  ".join(f"{r[k]:{a}{widths[k]}}" for k, _, a in cols))
    tz = _resolve_tz()
    for key, lbl in (("weekly_all", "Weekly (All models)"), ("session", "5h window")):
        segs = _cycles(con, key)
        if not segs:
            continue
        peaks = [max(s.percent) for s in segs]
        print(f"\n{lbl} — {len(segs)} cycle(s):")
        for i in range(max(0, len(segs) - 5), len(segs)):
            ini = segs[i].ts[0].astimezone(tz)
            d = peaks[i] - peaks[i - 1] if i > 0 else None
            delta = f"  ({'+' if d >= 0 else ''}{d:.0f} vs previous)" if d is not None else ""
            print(f"  {ini:%Y-%m-%d %H:%M %Z}  peak {peaks[i]:.0f}%{delta}")
        if len(peaks) >= 3:
            prev = peaks[:-1]
            avg = sum(prev) / len(prev)
            if avg > 0 and peaks[-1] > avg * 1.2:
                print(f"  ⚠ current cycle {peaks[-1]:.0f}% vs avg {avg:.0f}% (+{peaks[-1] / avg * 100 - 100:.0f}%)")


# --- Logs layer: mines ~/.claude/projects/**/*.jsonl for tokens & cost ---
LOGS_ROOT = os.path.expanduser("~/.claude/projects")
# USD per million tokens: (input, output, cache_read, cache_write). Estimate — adjust here.
# cache_read ≈ 0.1× input; cache_write = 2× input (1h TTL, what Claude Code uses).
PRICES = {
    "fable":  (10.0, 50.0, 1.00, 20.0),
    "opus":   (5.0, 25.0, 0.50, 10.0),
    "sonnet": (3.0, 15.0, 0.30, 6.0),
    "haiku":  (1.0, 5.0, 0.10, 2.0),
}


def _price(model: str):
    m = (model or "").lower()
    for k, p in PRICES.items():
        if k in m:
            return p
    return (0.0, 0.0, 0.0, 0.0)


def _short_model(model: str) -> str:
    m = (model or "").lower()
    for k in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if k in m:
            return k.capitalize()
    return model or "?"


SURFACE = {"cli": "terminal", "claude-vscode": "vscode", "claude-desktop": "app (desktop)",
           "sdk-cli": "sdk (-p/agent)"}


def _surface(entrypoint: str) -> str:
    return SURFACE.get(entrypoint, entrypoint or "?")


def _parse_jsonl(path: str):
    """Extract assistant messages with usage from JSONL transcript.
    Only json.loads lines containing '"usage"' — rest (user/tool) skipped cheaply.
    Claude Code writes one API response as several lines (thinking, text, tool_use), each
    repeating the same usage, and copies lines into resumed/forked transcripts — so the row
    key is the API message (message.id + requestId), not the line uuid; counting lines
    inflated burn ~2-3x. The line uuid rides along last, only for the legacy migration."""
    proj_fallback = os.path.basename(os.path.dirname(path))
    out = []
    try:
        f = open(path, "rb")  # binary + orjson: faster parse
    except OSError:
        return out
    with f:
        for line in f:
            if b'"usage"' not in line:  # pre-filter: avoids parsing most lines
                continue
            if len(line) > MAX_JSONL_LINE:  # DoS guard: don't parse a giant/deeply-nested line
                continue
            try:
                o = _loads(line)
            except Exception:
                continue
            msg = o.get("message")
            uid = o.get("uuid")
            if not isinstance(msg, dict) or not uid:
                continue
            u = msg.get("usage")
            ts = o.get("timestamp")
            if not isinstance(u, dict) or not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            cwd = o.get("cwd")
            proj = os.path.basename(cwd.rstrip("/")) if cwd else proj_fallback
            mid = msg.get("id")
            key = f"{mid}:{o.get('requestId') or ''}" if mid else uid
            out.append((key, dt, _safe(msg.get("model") or "?"), _safe(o.get("entrypoint") or "?"),
                        _safe(proj), _safe(o.get("sessionId") or "?"),
                        int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                        int(u.get("cache_read_input_tokens") or 0),
                        int(u.get("cache_creation_input_tokens") or 0), uid))
    return out


TOKEN_KEY_META = "token_log_key"  # meta marker: token_log rows keyed per API message ("v2")


def _ensure_token_tables(con, name: str = "token_log") -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {name}("
                "uuid TEXT PRIMARY KEY, ts TIMESTAMPTZ, model TEXT, entrypoint TEXT, "
                "project TEXT, session TEXT, "
                "in_tok BIGINT, out_tok BIGINT, cache_read BIGINT, cache_create BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS scanned(path TEXT PRIMARY KEY, mtime DOUBLE, size BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS session_account(session TEXT PRIMARY KEY, account TEXT)")


DESKTOP_SESSIONS = os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions")


def _terminal_account() -> str | None:
    """accountUuid the terminal's Claude Code is signed into, or None."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    try:
        with open(os.path.join(base, ".claude.json"), encoding="utf-8") as f:
            return (json.load(f).get("oauthAccount") or {}).get("accountUuid") or None
    except (OSError, ValueError, AttributeError):
        return None


def _sync_accounts(con) -> None:
    """Record the terminal account and map Desktop sessions to accounts. Desktop keeps one
    JSON per session under claude-code-sessions/<account>/<org>/, carrying the transcript's
    session id as cliSessionId. Mappings are kept after Desktop prunes its index."""
    import glob
    _meta_set(con, "terminal_account", _terminal_account() or "")
    pairs = {}
    for path in glob.glob(os.path.join(DESKTOP_SESSIONS, "*", "*", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                sid = json.load(f).get("cliSessionId")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sid, str) and sid:
            pairs[sid] = os.path.relpath(path, DESKTOP_SESSIONS).split(os.sep)[0]
    known = {r[0] for r in con.execute("SELECT session FROM session_account").fetchall()}
    new = [(k, v) for k, v in pairs.items() if k not in known]
    if new:
        con.executemany("INSERT INTO session_account VALUES (?, ?)", new)


def scan_logs(con, since=None, quiet: bool = False) -> None:
    """Incremental scan of JSONL -> token_log table. Caches by (mtime,size),
    deduplicates by uuid; with `since` skips files older than the window. Parse
    of new files runs in parallel (multicore)."""
    import glob
    # Migration: if token_log has old schema (no entrypoint), rebuild — it's cache.
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info('token_log')").fetchall()]
    except Exception:
        cols = []  # table doesn't exist yet (new database)
    if cols and "entrypoint" not in cols:
        con.execute("DROP TABLE token_log")
        con.execute("DROP TABLE IF EXISTS scanned")
    _ensure_token_tables(con)
    migrate = (_meta_get(con, TOKEN_KEY_META) or ("",))[0] != "v2"
    if migrate:  # rows keyed by line uuid: rescan every transcript still on disk (see below)
        con.execute("DELETE FROM scanned")
        since = None
    seen = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM scanned").fetchall()}
    since_epoch = since.timestamp() if since else None
    todo = []
    for f in glob.glob(os.path.join(LOGS_ROOT, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(f)
        except OSError:
            continue
        if seen.get(f) == (st.st_mtime, st.st_size):
            continue
        if since_epoch and st.st_mtime < since_epoch:
            continue  # outside window; don't mark as scanned so future scan picks it up
        todo.append((f, st))
    if not todo and not migrate:
        return
    _sync_accounts(con)
    if not quiet and todo:
        print(f"scanning {len(todo)} log file(s)…", file=sys.stderr)
    parsed = _parse_many([f for f, _ in todo])  # parallel (with serial fallback)
    rows = [r for chunk in parsed for r in chunk]
    if rows:
        _stage_token_rows(con, rows)
        if migrate:
            _migrate_token_keys(con)
        else:
            con.execute("INSERT INTO token_log SELECT * EXCLUDE (line_uuid) FROM _fresh "
                        "WHERE uuid NOT IN (SELECT uuid FROM token_log)")
        con.execute("DROP TABLE _fresh")
    elif migrate:
        _meta_set(con, TOKEN_KEY_META, "v2")
    if todo:
        con.executemany("INSERT INTO scanned VALUES (?, ?, ?) ON CONFLICT (path) DO UPDATE "
                        "SET mtime=excluded.mtime, size=excluded.size",
                        [(f, st.st_mtime, st.st_size) for f, st in todo])


def _stage_token_rows(con, rows) -> None:
    """Load parsed rows into temp table _fresh, one row per message key (the line with the
    most output wins). Goes through a private temp CSV: DuckDB binds Python list parameters
    at ~100 us per element, so the old unnest(?::T[]) insert cost seconds per watch tick
    and minutes on a full rescan; read_csv loads 120k rows in ~0.3 s."""
    import csv
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")  # 0600: rows carry project names/session ids
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for r in rows:
                w.writerow((r[0], r[1].timestamp(), *r[2:]))
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _fresh AS SELECT uuid, to_timestamp(ts) ts, model, entrypoint, "
            "project, \"session\", in_tok, out_tok, cache_read, cache_create, line_uuid "
            "FROM read_csv(?, header = false, quote = '\"', columns = {"
            "'uuid': 'TEXT', 'ts': 'DOUBLE', 'model': 'TEXT', 'entrypoint': 'TEXT', 'project': 'TEXT', "
            "'session': 'TEXT', 'in_tok': 'BIGINT', 'out_tok': 'BIGINT', 'cache_read': 'BIGINT', "
            "'cache_create': 'BIGINT', 'line_uuid': 'TEXT'}) "
            "QUALIFY row_number() OVER (PARTITION BY uuid ORDER BY out_tok DESC, ts) = 1", [path])
        # line uuids of every parsed line (not just the kept one) — the legacy keys they replace
        con.execute("CREATE OR REPLACE TEMP TABLE _lines AS SELECT DISTINCT line_uuid u FROM read_csv(?, "
                    "header = false, quote = '\"', all_varchar = true, column_names = ['k', 'ts', 'm', 'e', "
                    "'p', 's', 'i', 'o', 'r', 'c', 'line_uuid'])", [path])
    finally:
        os.unlink(path)


LEGACY_DEDUP_S = 600  # legacy rows: same model/surface/usage within this window = same message


def _migrate_token_keys(con) -> None:
    """One-off move from line-uuid keys to per-message keys (keys with ':'), rebuilding the
    table from _fresh (a full rescan) instead of deleting in place, which is slow under the
    primary-key index. Legacy rows whose line is still on disk are replaced by exact rows.
    The rest come from transcripts Claude Code already deleted — the only copy of that
    history — so they are deduplicated heuristically: a row repeating an earlier legacy row's
    model, surface and full usage tuple within LEGACY_DEDUP_S is the same message. Measured
    against exact keys on live transcripts this leaves ~+4% residual, versus 2-3x before."""
    con.execute("BEGIN")
    try:
        con.execute("DROP TABLE IF EXISTS token_log_new")
        _ensure_token_tables(con, "token_log_new")
        con.execute(
            "INSERT INTO token_log_new "
            "SELECT * EXCLUDE (line_uuid) FROM _fresh "
            "UNION ALL SELECT * FROM token_log WHERE uuid LIKE '%:%' AND uuid NOT IN (SELECT uuid FROM _fresh) "
            "UNION ALL SELECT * EXCLUDE (gap) FROM ("
            "  SELECT *, ts - lag(ts) OVER (PARTITION BY model, entrypoint, in_tok, out_tok, cache_read, "
            "  cache_create ORDER BY ts) gap FROM token_log "
            "  WHERE uuid NOT LIKE '%:%' AND uuid NOT IN (SELECT u FROM _lines)) "
            "WHERE gap IS NULL OR gap > to_seconds(?)", [LEGACY_DEDUP_S])
        con.execute("DROP TABLE token_log")
        con.execute("ALTER TABLE token_log_new RENAME TO token_log")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    _meta_set(con, TOKEN_KEY_META, "v2")


def _parse_many(paths: list) -> list:
    """Parse multiple JSONL in parallel (ProcessPool). Falls back to serial if pool fails
    or if there are few files (spawn overhead doesn't pay off)."""
    if len(paths) < 8:
        return [_parse_jsonl(p) for p in paths]
    try:
        from concurrent.futures import ProcessPoolExecutor
        workers = min(8, (os.cpu_count() or 2))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_parse_jsonl, paths, chunksize=16))
    except Exception:
        return [_parse_jsonl(p) for p in paths]


def _session_window_start(rows, now_utc):
    """Start of the CURRENT 5h rate-limit window (session resets_at − 5h), not a rolling
    now−5h — so burn/model-mix reflect only what you spent in this cycle. Falls back to
    now−5h when there's no session reset in rows."""
    for key, _lbl, _pct, reset, _a in rows:
        if key == "session" and reset:
            try:
                start = datetime.fromisoformat(reset) - timedelta(hours=5)
                return min(start, now_utc)  # guard: never a future start
            except (ValueError, TypeError):
                break
    return now_utc - timedelta(hours=5)


def _burn_rows(con, since):
    """Tokens by model since `since` as (model, in, out, cache_read, cache_create) tuples."""
    where = "WHERE ts >= ?" if since else ""
    return con.execute(
        f"SELECT model, sum(in_tok), sum(out_tok), sum(cache_read), sum(cache_create) "
        f"FROM token_terminal {where} GROUP BY model", [since] if since else []).fetchall()


def _burn_summary(rows):
    """(tokens_total, cost_usd, 'Opus 80% · Sonnet 20%') from _burn_rows, or None."""
    if not rows:
        return None
    tok, cost, bym = 0, 0.0, {}
    for model, i, o, r, c in rows:
        p = _price(model)
        cost += (i * p[0] + o * p[1] + r * p[2] + c * p[3]) / 1e6
        t = int(i + o + r + c)
        tok += t
        bym[_short_model(model)] = bym.get(_short_model(model), 0) + t
    mix = " · ".join(f"{k} {v / tok * 100:.0f}%" for k, v in sorted(bym.items(), key=lambda x: -x[1])) if tok else ""
    return tok, cost, mix


def burn(args):
    """Report tokens & estimated US$ from local Claude Code logs."""
    con = db()
    since = _parse_since(getattr(args, "since", None))
    scan_logs(con, since)
    col = {"model": "model", "surface": "entrypoint", "day": "CAST(ts AS DATE)",
           "project": "project", "session": "session"}[args.by]
    where = "WHERE ts >= ?" if since else ""
    src = "token_log" if getattr(args, "all_accounts", False) else "token_terminal"
    rows = con.execute(
        f"SELECT {col} grp, model, sum(in_tok), sum(out_tok), sum(cache_read), sum(cache_create) "
        f"FROM {src} {where} GROUP BY grp, model", [since] if since else []).fetchall()
    excluded = 0 if src == "token_log" else con.execute(
        f"SELECT coalesce(sum(in_tok + out_tok + cache_read + cache_create), 0) FROM token_log {where}",
        [since] if since else []).fetchone()[0] - sum(i + o + r + c for _g, _m, i, o, r, c in rows)
    if not rows:
        sys.exit("No Claude Code log data in period.")
    # Per-group tokens+cost, and per-component totals — cost needs the per-model price map,
    # so it's computed here in Python rather than in SQL.
    agg = {}  # grp -> [tok, custo]
    ctok = {"input": 0, "output": 0, "cache read": 0, "cache write": 0}
    ccost = {"input": 0.0, "output": 0.0, "cache read": 0.0, "cache write": 0.0}
    for grp, model, i, o, r, c in rows:
        p = _price(model)
        cost = (i * p[0] + o * p[1] + r * p[2] + c * p[3]) / 1e6
        a = agg.setdefault(grp, [0, 0.0])
        a[0] += i + o + r + c
        a[1] += cost
        ctok["input"] += i
        ctok["output"] += o
        ctok["cache read"] += r
        ctok["cache write"] += c
        ccost["input"] += i * p[0] / 1e6
        ccost["output"] += o * p[1] / 1e6
        ccost["cache read"] += r * p[2] / 1e6
        ccost["cache write"] += c * p[3] / 1e6
    ordered = sorted(agg.items(), key=lambda kv: -kv[1][1])  # by cost desc
    if getattr(args, "json", False):
        print(json.dumps([{"grp": g.isoformat() if hasattr(g, "isoformat") else g,
                            "tok": int(v[0]), "custo": v[1]} for g, v in ordered],
                         ensure_ascii=False))
        return
    per = {"model": "model", "surface": "surface", "day": "day",
           "project": "project", "session": "session"}[args.by]
    since_local = since.astimezone(_resolve_tz()) if since else None
    window = f" since {since_local:%Y-%m-%d %H:%M %Z}" if since_local else ""
    print(f"Consumption by {per}{window} (API equivalent):")
    lines = [(_surface(g) if args.by == "surface" else str(g), v[0] / 1e6, v[1])
             for g, v in ordered]
    w = max([len(lbl) for lbl, _, _ in lines] + [len("TOTAL")])  # widest label incl. long model ids
    for lbl, tok_m, cost in lines:
        print(f"  {lbl:<{w}} {tok_m:9.1f}M tok   US$ {cost:9.2f}")
    total_c = sum(v[1] for _, v in ordered)
    total_tok = sum(v[0] for _, v in ordered)
    print(f"  {'TOTAL':<{w}} {total_tok / 1e6:9.1f}M tok   US$ {total_c:9.2f}")

    # Breakdown by component: cache read usually dominates (context re-reading).
    print("\nBy component:")
    for k in ("input", "output", "cache read", "cache write"):
        pct = ccost[k] / total_c * 100 if total_c else 0
        print(f"  {k:12} {ctok[k] / 1e6:9.1f}M tok   US$ {ccost[k]:9.2f}   {pct:4.0f}%")
    real = ccost["input"] + ccost["output"]
    cache = ccost["cache read"] + ccost["cache write"]
    print(f"\n→ Real work (input+output): US$ {real:.0f} · Cache (context re-reading): US$ {cache:.0f}")
    print("\nAPI EQUIVALENT cost (pay-per-token) — you pay subscription, not this.")
    print("Estimate; Claude Code sessions of the terminal's account only (not claude.ai chat).")
    if excluded:
        print(f"Excluded {excluded / 1e6:.1f}M tok from Claude Desktop sessions of another or unknown "
              f"account (--all-accounts to include).")


def watch(args):
    """Live TUI: re-queries usage every N seconds and redraws; each read is saved to the
    database (deduped) so watching also builds history. Ctrl-C quits."""
    import random
    import time

    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    con = db()  # writable: records each read (deduped), plus _rate and burn from logs
    tz = _resolve_tz()  # resolved once, before Live(screen=True) takes over — any invalid-tz
                       # warning must print here, never inside render() (would corrupt the TUI)
    show_bar = getattr(args, "usage", False)
    base = float(args.interval)
    cur = base  # adaptive poll interval: grows on 403/errors, eases back on sustained success
    learned = _meta_get(con, "watch_interval")
    if learned and learned[1] < 7200:  # reuse an interval learned safe within the last 2h
        try:
            cur = max(base, min(float(learned[0]), 300.0))
        except (ValueError, TypeError):
            pass
    ok_streak = 0

    def color(pct: float) -> str:
        return "red" if pct >= 90 else "yellow" if pct >= 70 else "green"

    def render(cached=None):
        """cached=(rows, age_s) renders the last known snapshot without touching the network —
        the instant first frame; the fetch happens on the next loop pass. The endpoint can take
        30s+ (timeout+retry), and before this the terminal stayed blank the whole time."""
        if cached is None:
            try:
                rows, _ts = _snapshot(con)  # persists each read (deduped) via watch's own con
            except FetchError as e:
                return Panel(Text(f"{e}\nbacking off — next try in ~{cur:.0f}s", style="red"),
                             title="cmon watch — error", border_style="red"), False
        else:
            rows = cached[0]
        now_utc = datetime.now(UTC)
        t = Table(expand=True, header_style="bold")
        t.add_column("Window", ratio=None if show_bar else 1)
        if show_bar:
            t.add_column("Usage", ratio=1)
        t.add_column("%", justify="right")
        t.add_column("resets in", justify="right")
        t.add_column("rate", justify="right")
        t.add_column("projection", justify="right")
        proj_src = ""
        for key, lbl, pct, reset, act in rows:
            rate = _rate(con, key)
            rem_h = ((datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
                     if reset else None)
            proj = "-"
            if rate and rem_h and rem_h > 0:
                p, _eta, src = _project(con, pct, rate, rem_h, now_utc, tz, key)
                proj_src = proj_src or src
                proj = f"{min(p, 999):.0f}%"
            cells = [lbl + (" ●" if act else "")]
            if show_bar:
                cells += [Text(bar(pct, 28), style=color(pct)), f"{pct:.0f}"]
            else:  # without the bar the % cell carries the color cue
                cells.append(Text(f"{pct:.0f}", style=color(pct), justify="right"))
            t.add_row(*cells, fmt_eta(reset), f"{rate:.1f}%/h" if rate else "-", proj)
        body = [t]
        if note := PROJ_NOTE_WATCH.get(proj_src):
            body.append(Text(note, style="dim"))
        now_local = now_utc.astimezone(tz)
        if cached is not None:  # no scan/burn/alerts on the cached frame: keep it instant
            body.append(Text(f"{now_local:%H:%M:%S} · cached {cached[1]:.0f}s ago · updating…",
                             style="dim"))
            return Panel(Group(*body), title="cmon watch", border_style="cyan"), True
        win = _session_window_start(rows, now_utc)
        scan_logs(con, since=win, quiet=True)
        _log_forecasts(con, rows, tz, now_utc)  # deduped internally to FORECAST_EVERY_S
        _reconcile_forecasts(con)
        bs = _burn_summary(_burn_rows(con, win))
        if bs:
            tok, cost, mix = bs
            body.append(Text(f"burn 5h (logs): {tok / 1e6:.2f}M tok · $ {cost:.2f} · {mix}",
                             style="cyan"))
        alerts = _alerts(rows, con, tz)
        if alerts:
            body.append(Text("\n".join("⚠ " + m for m in alerts), style="bold red"))
        body.append(Text(f"{now_local:%H:%M:%S} · ~{cur:.0f}s · recording · ^C quits",
                         style="dim"))
        return Panel(Group(*body), title="cmon watch", border_style="cyan"), True

    got = _read_cache()
    if got and got[1] < 600:  # fresh enough to be an honest first frame
        panel, _ok = render(cached=got)
    else:
        panel = Panel(Text("connecting…", style="dim"), title="cmon watch", border_style="cyan")
    with Live(panel, console=Console(), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                panel, ok = render()
                live.update(panel)
                if ok:
                    ok_streak += 1
                    if ok_streak >= 5 and cur > base:  # stable again → ease back toward base
                        cur = max(base, cur / 1.5)
                        _meta_set(con, "watch_interval", str(cur))
                        ok_streak = 0
                else:  # 403/blocked/error → back off (cap 5 min), remember for next session
                    cur = min(cur * 1.8, 300.0)
                    ok_streak = 0
                    _meta_set(con, "watch_interval", str(cur))
                # jitter breaks the exact robotic cadence Cloudflare bot-detection flags
                time.sleep(cur * random.uniform(0.85, 1.15))
        except KeyboardInterrupt:
            pass


AGENT = "com.cmon.collect"  # macOS LaunchAgent / base name of the units


def _sched_cmd() -> list[str]:
    """Command that scheduler runs. Interpreter and script absolute + --db
    absolute: independent of PATH/cwd/env, which are minimal in launchd/cron/systemd."""
    return [sys.executable, os.path.abspath(__file__), "--db", os.path.abspath(DB),
            "collect", "--alert"]


def _install_macos(cmd, secs, logdir, dry):
    from xml.sax.saxutils import escape
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT}.plist")
    args_xml = "".join(f"    <string>{escape(x)}</string>\n" for x in cmd)
    content = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
               '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
               '<plist version="1.0"><dict>\n'
               f'  <key>Label</key><string>{AGENT}</string>\n'
               f'  <key>ProgramArguments</key><array>\n{args_xml}  </array>\n'
               f'  <key>StartInterval</key><integer>{secs}</integer>\n'
               '  <key>RunAtLoad</key><true/>\n'
               f'  <key>StandardOutPath</key><string>{logdir}/collect.out.log</string>\n'
               f'  <key>StandardErrorPath</key><string>{logdir}/collect.err.log</string>\n'
               '</dict></plist>\n')
    if dry:
        print(f"[dry-run] would write {plist}:\n{content}[dry-run] launchctl unload/load -w {plist}")
        return
    with open(plist, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    r = subprocess.run(["launchctl", "load", "-w", plist], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl load failed: {r.stderr.strip()}")
    print(f"✓ LaunchAgent installed: {plist}\n  logs in {logdir}. Uninstall: cmon uninstall")


def _install_linux(cmd, interval_min, dry):
    import shlex
    import shutil
    exec_line = " ".join(shlex.quote(x) for x in cmd)
    if shutil.which("systemctl"):
        d = os.path.expanduser("~/.config/systemd/user")
        service = (f"[Unit]\nDescription=cmon collect\n\n[Service]\nType=oneshot\n"
                   f"ExecStart={exec_line}\n")
        timer = (f"[Unit]\nDescription=cmon collect timer\n\n[Timer]\nOnBootSec=2min\n"
                 f"OnUnitActiveSec={interval_min}min\nPersistent=true\n\n"
                 f"[Install]\nWantedBy=timers.target\n")
        if dry:
            print(f"[dry-run] {d}/cmon-collect.service:\n{service}\n{d}/cmon-collect.timer:\n{timer}\n"
                  "[dry-run] systemctl --user daemon-reload && enable --now cmon-collect.timer")
            return
        os.makedirs(d, exist_ok=True)
        with open(f"{d}/cmon-collect.service", "w") as f:
            f.write(service)
        with open(f"{d}/cmon-collect.timer", "w") as f:
            f.write(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        r = subprocess.run(["systemctl", "--user", "enable", "--now", "cmon-collect.timer"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"systemctl failed: {r.stderr.strip()}")
        print(f"✓ systemd timer installed ({interval_min}min). View: systemctl --user list-timers")
    else:
        _cron(f"*/{interval_min} * * * * {exec_line}", dry)


def _cron(line, dry, remove=False):
    """Add/remove a crontab line marked with '# cmon'. Fallback without systemd."""
    tag = "# cmon-collect"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [ln for ln in cur.splitlines() if tag not in ln]
    new = kept if remove else kept + [f"{line}  {tag}"]
    if dry:
        print("[dry-run] crontab would become:\n" + "\n".join(new))
        return
    subprocess.run(["crontab", "-"], input="\n".join(new) + "\n", text=True)
    print("✓ crontab updated." if not remove else "✓ entry removed from crontab.")


def _install_windows(cmd, interval_min, dry):
    # A '"' can't appear in a valid Windows path anyway; reject it rather than emit a
    # broken schtasks /tr line (secperf F5) — matches the escaped Linux/macOS install paths.
    if any('"' in x for x in cmd):
        sys.exit("Refusing to schedule: an argument contains '\"', which breaks schtasks "
                 "/tr quoting. Use a --db path without double-quotes.")
    tr = " ".join(f'"{x}"' for x in cmd)
    a = ["schtasks", "/create", "/tn", "cmon-collect", "/tr", tr,
         "/sc", "minute", "/mo", str(interval_min), "/f"]
    if dry:
        print("[dry-run] " + subprocess.list2cmdline(a))
        return
    r = subprocess.run(a, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"schtasks failed: {r.stderr.strip()}")
    print(f"✓ Task 'cmon-collect' scheduled ({interval_min}min).")


def install(args):
    """Schedule 'cmon collect --alert' on OS native scheduler. --dry-run only shows."""
    logdir = os.path.expanduser("~/.cmon")
    if not args.dry_run:
        os.makedirs(logdir, exist_ok=True)
    cmd = _sched_cmd()
    m = args.interval
    print(f"Scheduling every {m}min · database {os.path.abspath(DB)}\n  {' '.join(cmd)}")
    if sys.platform == "darwin":
        _install_macos(cmd, m * 60, logdir, args.dry_run)
    elif sys.platform.startswith("linux"):
        _install_linux(cmd, m, args.dry_run)
    elif sys.platform.startswith("win"):
        _install_windows(cmd, m, args.dry_run)
    else:
        sys.exit(f"OS not supported for install: {sys.platform}")
    if not args.dry_run:
        print("Background token: uses OS vault or Claude Code credentials; "
              "shell 'CLAUDE_OAUTH_TOKEN' is NOT inherited. Run 'cmon token set' if needed.")


def uninstall(args):
    """Remove scheduling created by install."""
    if sys.platform == "darwin":
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT}.plist")
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        if os.path.exists(plist):
            os.remove(plist)
        print("✓ LaunchAgent removed.")
    elif sys.platform.startswith("linux"):
        import shutil
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "disable", "--now", "cmon-collect.timer"],
                           capture_output=True)
            d = os.path.expanduser("~/.config/systemd/user")
            for n in ("cmon-collect.timer", "cmon-collect.service"):
                p = f"{d}/{n}"
                if os.path.exists(p):
                    os.remove(p)
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            print("✓ systemd timer removed.")
        else:
            _cron("", dry=False, remove=True)
    elif sys.platform.startswith("win"):
        subprocess.run(["schtasks", "/delete", "/tn", "cmon-collect", "/f"], capture_output=True)
        print("✓ Task removed.")
    else:
        sys.exit(f"OS not supported: {sys.platform}")


def main():
    try:
        from dotenv import load_dotenv
        # cwd only: the no-path form walks every ancestor dir for a .env, letting a stray
        # parent .env inject HTTPS_PROXY / CMON_OAUTH_TOKEN_URL to exfil the token (secperf F1).
        load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
    except Exception:
        pass
    p = argparse.ArgumentParser(
        prog="cmon", description=__doc__.splitlines()[0],
        epilog="Token: env CLAUDE_OAUTH_TOKEN → OS vault (cmon token set) → "
               "Claude Code credentials. See 'cmon token --help'.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="DuckDB database path (overrides CMON_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pn = sub.add_parser("now", help="current usage + reset + rate/projection (--advice for pacing tips)")
    pn.add_argument("--advice", action="store_true",
                    help="append pacing tips: per-window targets, model mix, Claude Sonnet advice")
    pn.add_argument("--no-ai", action="store_true", help="with --advice: local projections only, skip Claude")
    ps = sub.add_parser("status", help="single line for statusline/tmux/prompt (reads local cache)")
    ps.add_argument("--live", action="store_true", help="query API instead of local cache")
    ps.add_argument("--json", action="store_true", help="versioned JSON for the menu bar/scripts")
    ptr = sub.add_parser("trends", help="consumption history: per-label summary + per-cycle peaks/anomaly")
    ptr.add_argument("--since", help="filter the summary from '24h', '7d' or ISO date")
    ptr.add_argument("--json", action="store_true", help="JSON output of the summary")
    pb = sub.add_parser("burn", help="tokens & estimated US$ from local Claude Code logs")
    pb.add_argument("--by", choices=["model", "surface", "day", "project", "session"], default="model",
                    help="group by model (default), surface (terminal/vscode/app/sdk), day, project or session")
    pb.add_argument("--since", default="30d",
                    help="window: '24h', '7d', '30d' (default), ISO date, or 'all' for everything")
    pb.add_argument("--json", action="store_true", help="JSON output")
    pb.add_argument("--all-accounts", action="store_true",
                    help="include Claude Desktop sessions not tied to the terminal's account")
    pc = sub.add_parser("collect", help="save 1 snapshot to database")
    pc.add_argument("--force", action="store_true", help="save even with recent snapshot (bypass dedup)")
    pc.add_argument("--alert", action="store_true",
                    help="alerts (stderr + desktop notification + CMON_HOOK): rate hits 100%% "
                         "before reset, or a window is within its lead of resetting "
                         "(CMON_ALERT_LEAD / CMON_ALERT_LEAD_WEEKLY min)")
    pw = sub.add_parser("watch", help="live TUI with current usage, recording each read (Ctrl-C quits)")
    pw.add_argument("-n", "--interval", type=int, default=75, help="seconds between updates (default 75)")
    pw.add_argument("-u", "--usage", action="store_true", help="show the Usage bar column")
    pwa = sub.add_parser("wait", help="block until window resets (or --at N%%), then notify")
    pwa.add_argument("--window", default="session", help="window to watch (default session = 5h; e.g., weekly_all)")
    pwa.add_argument("--at", type=float, help="instead of reset, wait for usage to reach N%%")
    pwa.add_argument("-n", "--interval", type=int, default=60, help="seconds between checks (default 60)")
    pbt = sub.add_parser("backtest", help="score past projections against what actually happened")
    pbt.add_argument("--since", help="only forecasts made after '24h', '7d' or an ISO date")
    pbt.add_argument("--replay", action="store_true",
                     help="score the existing history instead of live forecasts: rebuilds each "
                          "past prediction from the data available back then (no waiting)")
    pbt.add_argument("--json", action="store_true", help="JSON output")
    ptu = sub.add_parser("tune", help="fit the projection's parameters to your own history")
    ptu.add_argument("--show", action="store_true", help="print the active parameters and exit")
    ptu.add_argument("--reset", action="store_true", help="restore the built-in defaults")
    pin = sub.add_parser("install", help="schedule 'collect --alert' on OS scheduler (background collection)")
    pin.add_argument("-i", "--interval", type=int, default=20, help="minutes between collections (default 20)")
    pin.add_argument("--dry-run", action="store_true", help="show what it would do, don't install")
    sub.add_parser("uninstall", help="remove scheduling created by install")
    pim = sub.add_parser("import", help="merge history from other cmon databases (e.g. old ./usage.duckdb)")
    pim.add_argument("paths", nargs="+", metavar="PATH", help="source .duckdb files (opened read-only)")
    pp = sub.add_parser("plot", help="generate charts -> PNG (trajectory, pace-vs-target, burn)")
    pp.add_argument("-o", "--out", default=None,
                    help="PNG path (default: usage_YYMMDD_HHMMSS.png)")
    pp.add_argument("--since", default="30d",
                    help="burn panel window: '24h', '7d', '30d' (default), ISO date, or 'all'")

    pt = sub.add_parser("token", help="manage OAuth token securely (cross-platform)",
                        description="Stores token in OS native vault (Keychain on macOS, "
                                    "Credential Manager on Windows, Secret Service on Linux).")
    ta = pt.add_subparsers(dest="action", required=True)
    ta.add_parser("set", help="save token to OS vault (hidden input; accepts pipe)")
    ta.add_parser("status", help="show token source, masked")
    ta.add_parser("clear", help="remove saved token from vault")

    pcfg = sub.add_parser("config", help="manage persisted user preferences (~/.cmon/config.json)",
                          description="Currently supports: timezone (used to display timestamps "
                                      "locally instead of UTC).")
    cfa = pcfg.add_subparsers(dest="action", required=True)
    cfset = cfa.add_parser("set", help="set a preference")
    cfset.add_argument("key", choices=sorted(CONFIG_KEYS))
    cfset.add_argument("value", help="e.g. an IANA timezone name for 'timezone' (America/Sao_Paulo, UTC...)")
    cfget = cfa.add_parser("get", help="show a persisted preference")
    cfget.add_argument("key", choices=sorted(CONFIG_KEYS))
    cfa.add_parser("list", help="show all persisted preferences")
    cfclr = cfa.add_parser("clear", help="remove a preference (or --all)")
    cfclr.add_argument("key", nargs="?", choices=sorted(CONFIG_KEYS))
    cfclr.add_argument("--all", action="store_true", help="remove every persisted preference")

    args = p.parse_args()
    if args.db:
        global DB
        DB = args.db
    if args.cmd == "token":
        {"set": token_set, "status": token_status, "clear": token_clear}[args.action](args)
        return
    if args.cmd == "config":
        {"set": config_set, "get": config_get, "list": config_list,
         "clear": config_clear}[args.action](args)
        return
    try:
        {"now": now, "status": status, "trends": trends, "collect": collect,
         "burn": burn, "watch": watch, "wait": wait, "plot": plot,
         "backtest": backtest, "tune": tune, "install": install,
         "uninstall": uninstall, "import": import_db}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"cmon: {e}")


if __name__ == "__main__":
    main()
