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
  cmon token set   # save token to OS secure vault (cross-platform)
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

DB = os.environ.get("CMON_DB", "usage.duckdb")
URL = "https://claude.ai/api/oauth/usage"
# UA is required: without it claude.ai's Cloudflare returns 403 ("Just a moment").
UA = "claude-cli/1.0 (external, cli)"
LABELS = {"session": "Current session", "weekly_all": "All models"}
SERVICE, ACCOUNT = "cmon", "claude-oauth"  # entry in the OS secure vault (manual token set)
AUTO_ACCOUNT = "claude-oauth-auto"  # chain refreshed by cmon, separate from Claude Code
RETRIES = int(os.environ.get("CMON_RETRIES", "3"))
DEDUP_SECS = int(os.environ.get("CMON_DEDUP_SECS", "60"))  # window to deduplicate collect
ALERT_LEAD_MIN = int(os.environ.get("CMON_ALERT_LEAD", "60"))  # minutes-before-reset to alert (5h window)
MAX_JSONL_LINE = 4 << 20  # 4 MiB: skip parsing absurdly large transcript lines (secperf F4 DoS guard)
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
    con = duckdb.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS snapshots("
                "ts TIMESTAMPTZ, key TEXT, label TEXT, percent DOUBLE, "
                "resets_at TIMESTAMPTZ, is_active BOOL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT, ts TEXT)")
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
    """delta = percent - previous snapshot for same window; delta<0 = reset (not consumption)."""
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
    rem_h = (end - datetime.now(UTC)).total_seconds() / 3600
    proj = min(pct + rate * rem_h, 100)
    print(f"Rate: {rate:.1f}%/h → projection at reset: {proj:.0f}%.")
    if pct < 100:
        to100 = (100 - pct) / rate
        if to100 < rem_h:
            print(f"⚠ At current rate you'll hit 100% in ~{to100:.1f}h, before reset.")

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
    print(f"{ts:%Y-%m-%d %H:%M} collected:")
    for _k, lbl, pct, reset, _a in rows:
        print(f"  {lbl:16} {pct:4.0f}%  reset {reset[:16] if reset else '-'}")
    if getattr(args, "alert", False):
        for m in _alerts(rows, con):  # rate-based: at current pace hits 100% before reset
            print(f"⚠ {m}", file=sys.stderr)
            _notify("cmon — Claude limit", m)
        _fire_lead_alert(rows)  # time-based: 5h window resets in <= CMON_ALERT_LEAD min (+ hook)


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
    consumed % (sum of positive deltas). The headline table shown by 'trends'."""
    df = deltas(con)
    if since is not None:
        df = df[df.ts >= since]
        if df.empty:
            sys.exit(f"No data since {since:%Y-%m-%d %H:%M} UTC.")
    return df.groupby("label").agg(
        snapshots=("percent", "size"),
        peak_pct=("percent", "max"),
        consumed_pct=("delta", lambda s: s[s > 0].sum())).round().astype(int)


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
    import pandas as pd
    segs = _cycles(con, key)
    if not segs or len(segs[-1]) < 1:
        ax.text(0.5, 0.5, "no data yet", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title)
        return
    seg = segs[-1]  # current cycle (after last reset)
    ax.plot(seg.ts, seg.percent, marker="o", label="actual")
    # .df() not .fetchone(): scalar TIMESTAMPTZ->python needs pytz; the DataFrame path doesn't.
    rdf = con.execute("SELECT resets_at FROM snapshots WHERE key=? AND resets_at IS NOT NULL "
                      "ORDER BY ts DESC LIMIT 1", [key]).df()
    if not rdf.empty and rdf.resets_at.iloc[0] is not None:
        reset = pd.Timestamp(rdf.resets_at.iloc[0])
        x0, y0 = seg.ts.iloc[0], float(seg.percent.iloc[0])
        ax.plot([x0, reset], [y0, 100], ls="--", color="gray", label="even pace → 100% at reset")
        ax.axvline(reset, color="red", ls=":", lw=1)
        now_ts = pd.Timestamp(now_utc)
        if x0 <= now_ts <= reset:
            ax.axvline(now_ts, color="green", ls=":", lw=1, label="now")
    ax.set_ylim(0, 105)
    ax.set_title(title)
    ax.set_xlabel("")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(_time_formatter(seg.ts.dt.tz))
    ax.tick_params(axis="x", rotation=30)


def _burn_ax(ax, con, since) -> None:
    """Stacked API-equivalent cost/day by model from token_log — the richest table (where the
    tokens actually go). You pay a subscription, not this; it's for relative comparison."""
    scan_logs(con, since=since, quiet=True)
    where = "WHERE ts >= ?" if since else ""
    df = con.execute(
        f"SELECT CAST(ts AS DATE) d, model, sum(in_tok) i, sum(out_tok) o, "
        f"sum(cache_read) r, sum(cache_create) c FROM token_log {where} GROUP BY d, model",
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


def plot(args):
    try:
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        sys.exit("Plots need plot extras. Install with:\n"
                 "  uv sync --extra plot         (in repo)\n"
                 "  pip install 'cmon[plot]'     (via PyPI)")
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


def _rate(con, key) -> float | None:
    """Weighted %/h in the current window (segment since the last reset). Exponentially
    weighted least-squares slope: recent snapshots dominate (half-life ~1h for the 5h window,
    ~12h weekly), so the projection tracks your current pace instead of a flat cycle average.
    None if there's no net rise to project. Self-adapts to 5h/7d/etc. by cutting at each reset."""
    if con is None:
        return None
    df = con.execute("SELECT ts, percent FROM snapshots WHERE key=? ORDER BY ts", [key]).df()
    if len(df) < 2:
        return None
    drop_pos = (df.percent.diff() < 0).to_numpy().nonzero()[0]  # vectorized reset detection
    seg = df.iloc[drop_pos[-1]:] if len(drop_pos) else df  # only the segment after last reset
    if len(seg) < 2:
        return None
    import numpy as np
    # t = hours relative to the newest sample (<=0); exp(t/half_life) decays weight into the past.
    t = (seg.ts - seg.ts.iloc[-1]).dt.total_seconds().to_numpy() / 3600.0
    y = seg.percent.to_numpy(dtype=float)
    half_life = 1.0 if key == "session" else 12.0
    w = np.exp(t / half_life)
    sw = w.sum()
    tm = (w * t).sum() / sw
    denom = (w * (t - tm) ** 2).sum()
    if denom <= 0:  # all samples effectively at one instant
        return None
    ym = (w * y).sum() / sw
    slope = (w * (t - tm) * (y - ym)).sum() / denom  # %/h, exponentially weighted
    return float(slope) if slope > 0 else None


def _alerts(rows, con) -> list[str]:
    """Alert when, at current rate, a window hits 100% before reset.
    Needs history (via _rate); without database/rate produces nothing."""
    now_utc = datetime.now(UTC)
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
        eta = (100 - pct) / rate
        if eta < rem_h:
            msgs.append(f"{lbl}: at rate {rate:.1f}%/h hits 100% in ~{eta:.1f}h "
                        f"(~{rem_h - eta:.1f}h before reset).")
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


def _lead_alert(rows) -> str | None:
    """Message if the 5h (session) window is within CMON_ALERT_LEAD minutes of resetting, else
    None. Purely time-based (uses resets_at), independent of consumption rate."""
    sess = next((r for r in rows if r[0] == "session"), None)
    if not sess or not sess[3]:
        return None
    reset = datetime.fromisoformat(sess[3]) if isinstance(sess[3], str) else sess[3]
    rem_min = (reset - datetime.now(UTC)).total_seconds() / 60
    if 0 < rem_min <= ALERT_LEAD_MIN:
        return f"5h window resets in ~{rem_min:.0f}m (used {sess[2]:.0f}%)."
    return None


def _fire_lead_alert(rows) -> None:
    """Fire the time-based 5h alert once per cycle: stderr + native notification + CMON_HOOK.
    Deduped on the session reset bucketed to the nearest minute — the API's resets_at jitters
    ~1s between calls, so an exact-string marker would re-fire every collect."""
    msg = _lead_alert(rows)
    if not msg:
        return
    reset = next((r[3] for r in rows if r[0] == "session"), None)
    if not reset:
        return
    rdt = datetime.fromisoformat(reset) if isinstance(reset, str) else reset
    marker = round(rdt.timestamp() / 60)  # nearest-minute bucket, robust to ~1s API jitter
    if _alert_fired("lead_session", marker):
        return
    print(f"⚠ {msg}", file=sys.stderr)
    _notify("cmon — 5h window", msg)
    _run_hook(msg)
    _mark_alert("lead_session", marker)


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


def _window_tips(lbl: str, pct: float, reset: str | None, rate: float | None, now_utc):
    """Deterministic pacing block for a window. Returns (printed_lines, summary_line)."""
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
    proj = pct + rate * rem_h
    summ += f", rate {rate:.2f}%/h, projection {min(proj, 999):.0f}%"
    out.append(f"  current rate {rate:.2f}%/h → projection at reset ~{min(proj, 999):.0f}%")
    if proj < 97:
        gap = tgt - rate
        extra = f" (~+{gap:.2f}%/h)" if gap > 0 else ""
        out.append(f"  ↑ upside: ~{100 - proj:.0f}% left on table — can intensify{extra} "
                   "or use stronger model")
    elif proj > 103:
        eta = (100 - pct) / rate
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
    summaries = []
    for key, want in (("weekly_all", "Weekly (All models)"), ("session", "5h window")):
        row = next((r for r in rows if r[0] == key), None)
        if not row:
            continue
        _k, _lbl, pct, reset, _a = row
        block, summ = _window_tips(want, pct, reset, _rate(con, key), now_utc)
        print("\n".join(block) + "\n")
        summaries.append(summ)

    # scoped models (e.g., Fable/Opus) go only in summary so AI can suggest swap
    for key, lbl, pct, reset, _a in rows:
        if key not in ("weekly_all", "session"):
            summaries.append(f"- {lbl}: {pct:.0f}% used, resets in {fmt_eta(reset)}")

    # real model mix in last 5h (from logs) — grounds model swap advice; skipped if DB busy
    if con is not None:
        win = now_utc - timedelta(hours=5)
        scan_logs(con, since=win, quiet=True)
        bs = _burn_summary(_burn_rows(con, win))
        if bs:
            tok, cost, mix = bs
            line = f"Model mix (last 5h): {tok / 1e6:.2f}M tok · US$ {cost:.2f} · {mix}"
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
    df = con.execute("SELECT key,label,percent,resets_at,is_active,ts FROM snapshots "
                     "WHERE ts=(SELECT max(ts) FROM snapshots)").df()
    if df.empty:
        return None
    rows = [(r.key, r.label, float(r.percent),
             r.resets_at.isoformat() if r.resets_at is not None else None,
             bool(r.is_active)) for r in df.itertuples()]
    return rows, (datetime.now(UTC) - df.ts.iloc[0]).total_seconds()


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


def status(args):
    """Single line for statusline/tmux/prompt. By default reads local cache (fast,
    ~sub-20ms, no network) fed by 'collect'; --live forces API. A cache whose session
    window already reset is auto-refreshed from the API (and re-cached) so the line never
    shows a dead 'reset expired' cycle; on network failure it falls back to the stale
    rows with an 'Xm ago' marker."""
    age = None
    if args.live:
        try:
            rows = limits(fetch())
            _write_cache(rows, datetime.now(UTC))  # warm the cache so plain 'status' stays fresh
        except FetchError:
            print("cmon offline")
            return
    else:
        got = _read_cache() or _rows_from_db()
        if got and not _reset_expired(got[0]):
            rows, age = got
        else:
            try:
                rows = limits(fetch(retries=1))  # no/stale cache: refresh and re-cache
                _write_cache(rows, datetime.now(UTC))
            except FetchError:
                if got:  # offline: keep stale rows; staleness shown via 'Xm ago'
                    rows, age = got
                else:
                    print("cmon offline")
                    return
    parts = []
    for key, short in (("session", "5h"), ("weekly_all", "wk")):
        r = next((x for x in rows if x[0] == key), None)
        if r:
            parts.append(f"{short} {r[2]:.0f}%")
    sess = next((x for x in rows if x[0] == "session"), None)
    if sess and sess[3]:
        parts.append(f"reset {_eta_short(sess[3])}")
    if age is not None and age > 1800:  # stale data: signal without noise
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
    (percent drop). Returns list of DataFrames (ts, percent)."""
    if con is None:
        return []
    df = con.execute("SELECT ts, percent FROM snapshots WHERE key=? ORDER BY ts", [key]).df()
    if df.empty:
        return []
    cut_pos = (df.percent.diff() < 0).to_numpy().nonzero()[0]  # vectorized reset boundaries
    bounds = [0, *cut_pos.tolist(), len(df)]
    return [df.iloc[a:b] for a, b in zip(bounds, bounds[1:], strict=False) if a < b]


def trends(args):
    """Consumption history: a per-label summary (snapshots/peak/total consumed over --since,
    --json for scripts) followed by the reset-aware per-cycle breakdown — peak per cycle,
    delta vs. previous, and an anomaly alert if the current cycle exceeds the average."""
    con = db(create=False)
    if con is None:
        sys.exit("No history — run 'cmon collect' a few times first.")
    g = _summary(con, _parse_since(getattr(args, "since", None)))
    if getattr(args, "json", False):
        print(g.reset_index().to_json(orient="records", force_ascii=False))
        return
    print(g.to_string())
    for key, lbl in (("weekly_all", "Weekly (All models)"), ("session", "5h window")):
        segs = _cycles(con, key)
        if not segs:
            continue
        peaks = [float(s.percent.max()) for s in segs]
        print(f"\n{lbl} — {len(segs)} cycle(s):")
        for i in range(max(0, len(segs) - 5), len(segs)):
            ini = segs[i].ts.iloc[0]
            d = peaks[i] - peaks[i - 1] if i > 0 else None
            delta = f"  ({'+' if d >= 0 else ''}{d:.0f} vs previous)" if d is not None else ""
            print(f"  {ini:%Y-%m-%d %H:%M}  peak {peaks[i]:.0f}%{delta}")
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
    for k in ("opus", "sonnet", "haiku"):
        if k in m:
            return k.capitalize()
    return model or "?"


SURFACE = {"cli": "terminal", "claude-vscode": "vscode", "claude-desktop": "app (desktop)",
           "sdk-cli": "sdk (-p/agent)"}


def _surface(entrypoint: str) -> str:
    return SURFACE.get(entrypoint, entrypoint or "?")


def _parse_jsonl(path: str):
    """Extract assistant messages with usage from JSONL transcript.
    Only json.loads lines containing '"usage"' — rest (user/tool) skipped cheaply."""
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
            out.append((uid, dt, _safe(msg.get("model") or "?"), _safe(o.get("entrypoint") or "?"),
                        _safe(proj), _safe(o.get("sessionId") or "?"),
                        int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                        int(u.get("cache_read_input_tokens") or 0),
                        int(u.get("cache_creation_input_tokens") or 0)))
    return out


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
    con.execute("CREATE TABLE IF NOT EXISTS token_log("
                "uuid TEXT PRIMARY KEY, ts TIMESTAMPTZ, model TEXT, entrypoint TEXT, "
                "project TEXT, session TEXT, "
                "in_tok BIGINT, out_tok BIGINT, cache_read BIGINT, cache_create BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS scanned(path TEXT PRIMARY KEY, mtime DOUBLE, size BIGINT)")
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
    if not todo:
        return
    if not quiet:
        print(f"scanning {len(todo)} log file(s)…", file=sys.stderr)
    import pandas as pd
    parsed = _parse_many([f for f, _ in todo])  # parallel (with serial fallback)
    rows = [r for chunk in parsed for r in chunk]
    if rows:
        # Vectorized insert via DataFrame: ~680x faster than executemany+ON CONFLICT.
        # drop_duplicates resolves duplicate uuids within batch (mirrored transcripts).
        tdf = pd.DataFrame(rows, columns=["uuid", "ts", "model", "entrypoint", "project",
                                          "session", "in_tok", "out_tok", "cache_read",
                                          "cache_create"]).drop_duplicates("uuid")
        con.register("_tl_new", tdf)
        con.execute("INSERT INTO token_log SELECT * FROM _tl_new ORDER BY ts ON CONFLICT DO NOTHING")
        con.unregister("_tl_new")
    sdf = pd.DataFrame([[f, st.st_mtime, st.st_size] for f, st in todo],
                       columns=["path", "mtime", "size"])
    con.register("_sc_new", sdf)
    con.execute("INSERT INTO scanned SELECT * FROM _sc_new ON CONFLICT (path) DO UPDATE "
                "SET mtime=excluded.mtime, size=excluded.size")
    con.unregister("_sc_new")


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


def _burn_rows(con, since):
    """Tokens by (window, model) since `since`, already with estimated cost per row."""
    where = "WHERE ts >= ?" if since else ""
    df = con.execute(
        f"SELECT model, sum(in_tok) i, sum(out_tok) o, sum(cache_read) r, sum(cache_create) c "
        f"FROM token_log {where} GROUP BY model", [since] if since else []).df()
    return df


def _burn_summary(df):
    """(tokens_total, cost_usd, 'Opus 80% · Sonnet 20%') from _burn_rows, or None."""
    if df.empty:
        return None
    tok, cost, bym = 0, 0.0, {}
    for _, r in df.iterrows():
        p = _price(r.model)
        cost += (r.i * p[0] + r.o * p[1] + r.r * p[2] + r.c * p[3]) / 1e6
        t = int(r.i + r.o + r.r + r.c)
        tok += t
        bym[_short_model(r.model)] = bym.get(_short_model(r.model), 0) + t
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
    df = con.execute(
        f"SELECT {col} grp, model, sum(in_tok) i, sum(out_tok) o, sum(cache_read) r, sum(cache_create) c "
        f"FROM token_log {where} GROUP BY grp, model", [since] if since else []).df()
    if df.empty:
        sys.exit("No Claude Code log data in period.")
    df["custo"] = [
        (row.i * _price(row.model)[0] + row.o * _price(row.model)[1]
         + row.r * _price(row.model)[2] + row.c * _price(row.model)[3]) / 1e6
        for row in df.itertuples()]
    df["tok"] = df.i + df.o + df.r + df.c
    g = df.groupby("grp").agg(tok=("tok", "sum"), custo=("custo", "sum")).reset_index()
    g = g.sort_values("custo", ascending=False)
    if getattr(args, "json", False):
        print(g.to_json(orient="records", force_ascii=False))
        return
    per = {"model": "model", "surface": "surface", "day": "day",
           "project": "project", "session": "session"}[args.by]
    window = f" since {since:%Y-%m-%d %H:%M} UTC" if since else ""
    print(f"Consumption by {per}{window} (API equivalent):")
    lines = [(_surface(row.grp) if args.by == "surface" else str(row.grp),
              row.tok / 1e6, row.custo) for row in g.itertuples()]
    w = max([len(lbl) for lbl, _, _ in lines] + [len("TOTAL")])  # widest label incl. long model ids
    for lbl, tok_m, cost in lines:
        print(f"  {lbl:<{w}} {tok_m:9.1f}M tok   US$ {cost:9.2f}")
    total_c = g.custo.sum()
    print(f"  {'TOTAL':<{w}} {g.tok.sum() / 1e6:9.1f}M tok   US$ {total_c:9.2f}")

    # Breakdown by component: cache read usually dominates (context re-reading).
    comp = {"input": (0, 0.0, 0), "output": (0, 0.0, 1),
            "cache read": (0, 0.0, 2), "cache write": (0, 0.0, 3)}
    tok_by = {"input": "i", "output": "o", "cache read": "r", "cache write": "c"}
    ctok = {k: int(df[v].sum()) for k, v in tok_by.items()}
    ccost = {k: 0.0 for k in comp}
    for row in df.itertuples():
        p = _price(row.model)
        ccost["input"] += row.i * p[0] / 1e6
        ccost["output"] += row.o * p[1] / 1e6
        ccost["cache read"] += row.r * p[2] / 1e6
        ccost["cache write"] += row.c * p[3] / 1e6
    print("\nBy component:")
    for k in ("input", "output", "cache read", "cache write"):
        pct = ccost[k] / total_c * 100 if total_c else 0
        print(f"  {k:12} {ctok[k] / 1e6:9.1f}M tok   US$ {ccost[k]:9.2f}   {pct:4.0f}%")
    real = ccost["input"] + ccost["output"]
    cache = ccost["cache read"] + ccost["cache write"]
    print(f"\n→ Real work (input+output): US$ {real:.0f} · Cache (context re-reading): US$ {cache:.0f}")
    print("\nAPI EQUIVALENT cost (pay-per-token) — you pay subscription, not this.")
    print("Estimate; Claude Code CLI usage only (doesn't include claude.ai web/desktop).")


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

    def render():
        try:
            rows, _ts = _snapshot(con)  # persists each read (deduped) via watch's own con
        except FetchError as e:
            return Panel(Text(f"{e}\nbacking off — next try in ~{cur:.0f}s", style="red"),
                         title="cmon watch — error", border_style="red"), False
        now_utc = datetime.now(UTC)
        t = Table(expand=True, header_style="bold")
        t.add_column("Window")
        t.add_column("Usage", ratio=1)
        t.add_column("%", justify="right")
        t.add_column("resets in", justify="right")
        t.add_column("rate", justify="right")
        t.add_column("projection", justify="right")
        for key, lbl, pct, reset, act in rows:
            rate = _rate(con, key)
            rem_h = ((datetime.fromisoformat(reset) - now_utc).total_seconds() / 3600
                     if reset else None)
            proj = f"{min(pct + rate * rem_h, 999):.0f}%" if rate and rem_h and rem_h > 0 else "-"
            t.add_row(lbl + (" ●" if act else ""),
                      Text(bar(pct, 28), style=color(pct)),
                      f"{pct:.0f}", fmt_eta(reset),
                      f"{rate:.1f}%/h" if rate else "-", proj)
        body = [t]
        win = now_utc - timedelta(hours=5)
        scan_logs(con, since=win, quiet=True)
        bs = _burn_summary(_burn_rows(con, win))
        if bs:
            tok, cost, mix = bs
            body.append(Text(f"burn 5h (logs): {tok / 1e6:.2f}M tok · US$ {cost:.2f} · {mix}",
                             style="cyan"))
        alerts = _alerts(rows, con)
        if alerts:
            body.append(Text("\n".join("⚠ " + m for m in alerts), style="bold red"))
        body.append(Text(f"{now_utc:%H:%M:%S} UTC · every ~{cur:.0f}s"
                         " · recording · Ctrl-C quits", style="dim"))
        return Panel(Group(*body), title="cmon watch", border_style="cyan"), True

    panel, _ok = render()
    with Live(panel, console=Console(), screen=True, refresh_per_second=4) as live:
        try:
            while True:
                # jitter breaks the exact robotic cadence Cloudflare bot-detection flags
                time.sleep(cur * random.uniform(0.85, 1.15))
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
    ptr = sub.add_parser("trends", help="consumption history: per-label summary + per-cycle peaks/anomaly")
    ptr.add_argument("--since", help="filter the summary from '24h', '7d' or ISO date")
    ptr.add_argument("--json", action="store_true", help="JSON output of the summary")
    pb = sub.add_parser("burn", help="tokens & estimated US$ from local Claude Code logs")
    pb.add_argument("--by", choices=["model", "surface", "day", "project", "session"], default="model",
                    help="group by model (default), surface (terminal/vscode/app/sdk), day, project or session")
    pb.add_argument("--since", default="30d",
                    help="window: '24h', '7d', '30d' (default), ISO date, or 'all' for everything")
    pb.add_argument("--json", action="store_true", help="JSON output")
    pc = sub.add_parser("collect", help="save 1 snapshot to database")
    pc.add_argument("--force", action="store_true", help="save even with recent snapshot (bypass dedup)")
    pc.add_argument("--alert", action="store_true",
                    help="alerts (stderr + notification + CMON_HOOK): rate hits 100%% before "
                         "reset, or 5h window resets within CMON_ALERT_LEAD min")
    pw = sub.add_parser("watch", help="live TUI with current usage, recording each read (Ctrl-C quits)")
    pw.add_argument("-n", "--interval", type=int, default=30, help="seconds between updates (default 30)")
    pwa = sub.add_parser("wait", help="block until window resets (or --at N%%), then notify")
    pwa.add_argument("--window", default="session", help="window to watch (default session = 5h; e.g., weekly_all)")
    pwa.add_argument("--at", type=float, help="instead of reset, wait for usage to reach N%%")
    pwa.add_argument("-n", "--interval", type=int, default=60, help="seconds between checks (default 60)")
    pin = sub.add_parser("install", help="schedule 'collect --alert' on OS scheduler (background collection)")
    pin.add_argument("-i", "--interval", type=int, default=20, help="minutes between collections (default 20)")
    pin.add_argument("--dry-run", action="store_true", help="show what it would do, don't install")
    sub.add_parser("uninstall", help="remove scheduling created by install")
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

    args = p.parse_args()
    if args.db:
        global DB
        DB = args.db
    if args.cmd == "token":
        {"set": token_set, "status": token_status, "clear": token_clear}[args.action](args)
        return
    try:
        {"now": now, "status": status, "trends": trends, "collect": collect,
         "burn": burn, "watch": watch, "wait": wait, "plot": plot,
         "install": install, "uninstall": uninstall}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"cmon: {e}")


if __name__ == "__main__":
    main()
