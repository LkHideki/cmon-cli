#!/bin/sh
# Backs 'make install', 'make uninstall' and 'make doctor'. Everything stays at user level:
# the cmon command via uv tool, ~/Applications/cmon Bar.app, ~/Library/LaunchAgents, ~/.cmon.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BAR_JSON="$HOME/.cmon/bar.json"
APP="$HOME/Applications/cmon Bar.app"
OLD_LAYOUT='["{5h}","{wk}","{reset}","{wk_reset_h}"]'
NEW_LAYOUT='["{5h_pair}","{wk_pair}"]'
LAYOUT_MARK="$HOME/.cmon/.bar-layout-pairs"
DB_BACKUP="$HOME/.cmon/usage.before-provider-schema.duckdb"

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\nError: %s\n' "$1" >&2; exit 1; }

cmon_path() { printf '%s/cmon' "$(uv tool dir --bin)"; }

check_prereqs() {
    [ "$(uname -s)" = Darwin ] || die "cmon Bar is macOS only. For the CLI alone: uv tool install ."
    major="$(sw_vers -productVersion | cut -d. -f1)"
    [ "$major" -ge 14 ] || die "cmon Bar needs macOS 14 or newer (this is $(sw_vers -productVersion))."
    command -v uv >/dev/null || die "uv not found. Install it, open a new terminal and run make install again:
  curl -LsSf https://astral.sh/uv/install.sh | sh"
    xcrun --find swiftc >/dev/null 2>&1 || die "Swift not found. Install the Command Line Tools, then run make install again:
  xcode-select --install"
}

# Once only: the new schema can't be read by older cmon versions, so keep a way back.
backup_db() {
    [ -f "$HOME/.cmon/usage.duckdb" ] && [ ! -e "$DB_BACKUP" ] || return 0
    cp -p "$HOME/.cmon/usage.duckdb" "$DB_BACKUP"
    [ ! -f "$HOME/.cmon/usage.duckdb.wal" ] || cp -p "$HOME/.cmon/usage.duckdb.wal" "$DB_BACKUP.wal"
    echo "History backed up to $DB_BACKUP"
}

# Once only: a bar.json still holding the old default gets the new layout; any other
# layout, or one restored after this ran, is the user's choice and stays.
configure_bar() {
    mkdir -p "$HOME/.cmon"
    chmod 700 "$HOME/.cmon"
    if [ -f "$BAR_JSON" ] && [ ! -e "$LAYOUT_MARK" ]; then
        current="$(plutil -extract segments json -o - "$BAR_JSON" 2>/dev/null || true)"
        if [ "$current" = "$OLD_LAYOUT" ]; then
            plutil -replace segments -json "$NEW_LAYOUT" "$BAR_JSON"
            plutil -replace separator -string "  " "$BAR_JSON"
            echo "Menu bar title now reads: 2% 2  44% 91"
        fi
    fi
    : > "$LAYOUT_MARK"
    # cmon Bar looks in ~/.local/bin by default; uv may be configured to use another directory.
    bin="$(cmon_path)"
    if [ "$bin" != "$HOME/.local/bin/cmon" ]; then
        [ -f "$BAR_JSON" ] || echo '{}' > "$BAR_JSON"
        plutil -replace cmon -string "$bin" "$BAR_JSON"
    fi
    [ ! -f "$BAR_JSON" ] || chmod 600 "$BAR_JSON"
}

do_install() {
    check_prereqs
    say "Installing the cmon command"
    backup_db
    uv tool install --force --reinstall --quiet "$ROOT"
    case ":$PATH:" in
        *":$(uv tool dir --bin):"*) ;;
        *) uv tool update-shell --quiet || true ;;
    esac
    say "Scheduling background collection"
    "$(cmon_path)" install
    say "Building cmon Bar"
    configure_bar
    "$ROOT/menubar/install.sh"
    # In the foreground: macOS may ask to allow Keychain access, which needs someone at the screen.
    say "Reading your usage"
    "$(cmon_path)" status --live || true
    if do_doctor; then printf '\nDone. '; else printf '\nInstalled, but fix the items marked FAIL above, then run: make doctor\n'; fi
    cat <<MSG
How to read the menu bar: 90% 75'  44% 91
  left group:  5h window, 90% used, 75 minutes until it resets
  right group: weekly limit, 44% used, 91 hours until it resets
  Plain numbers are hours, ' is minutes, " is seconds.
Click the title for details. Settings: ~/.cmon/bar.json. Remove all: make uninstall
MSG
}

do_uninstall() {
    bin="$(command -v uv >/dev/null && cmon_path || true)"
    [ ! -x "${bin:-}" ] || "$bin" uninstall || true
    "$ROOT/menubar/install.sh" uninstall
    ! command -v uv >/dev/null || uv tool uninstall cmon-cli || true
    echo "Removed. Your history and settings are still in ~/.cmon (delete that folder to erase them)."
}

ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n        -> %s\n' "$1" "$2"; problems=$((problems + 1)); }

do_doctor() {
    problems=0
    say "Checking the installation"
    if ! command -v uv >/dev/null; then
        bad "uv" "curl -LsSf https://astral.sh/uv/install.sh | sh"
        printf '\n%s problem(s) found.\n' "$problems"
        return 1
    fi
    ok "uv"
    bin="$(cmon_path)"
    if [ -x "$bin" ]; then ok "cmon command ($bin)"; else bad "cmon command" "make install"; fi
    case ":$PATH:" in
        *":$(dirname "$bin"):"*) ok "cmon on PATH" ;;
        *) printf '  note  open a new terminal to use the cmon command (cmon Bar does not need this)\n' ;;
    esac
    if [ -d "$APP" ]; then ok "cmon Bar.app"; else bad "cmon Bar.app" "make install"; fi
    if launchctl print "gui/$(id -u)/com.cmon.bar" >/dev/null 2>&1; then ok "cmon Bar starts at login"
    else bad "cmon Bar starts at login" "make install"; fi
    if pgrep -f "$APP/Contents/MacOS/CmonBar" >/dev/null; then ok "cmon Bar running"
    else bad "cmon Bar running" "open \"$APP\""; fi
    # -convert, not -lint: -lint rejects valid JSON.
    if [ ! -f "$BAR_JSON" ] || plutil -convert xml1 -o /dev/null "$BAR_JSON" >/dev/null 2>&1; then ok "bar.json is valid"
    else bad "bar.json is valid" "fix $BAR_JSON, or delete it to get the defaults"; fi
    if [ -x "$bin" ]; then
        if "$bin" status --json --live 2>/dev/null | grep -q '"offline": false'; then ok "usage API reachable and logged in"
        else bad "usage API reachable and logged in" "run Claude Code once to log in, then: cmon token status"; fi
    fi
    if [ "$problems" -eq 0 ]; then printf '\nAll good.\n'; return 0; fi
    printf '\n%s problem(s) found.\n' "$problems"
    return 1
}

case "${1:-}" in
    install) do_install ;;
    uninstall) do_uninstall ;;
    doctor) do_doctor ;;
    *) die "usage: $0 install|uninstall|doctor" ;;
esac
