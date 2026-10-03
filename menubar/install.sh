#!/bin/sh
# Build cmon Bar and start it at login (macOS). Usage: menubar/install.sh [uninstall]
# Needs the Xcode Command Line Tools (swiftc) and a cmon with 'status --json'.
set -eu

LABEL="com.cmon.bar"
APP="$HOME/Applications/cmon Bar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SRC="$(cd "$(dirname "$0")" && pwd)/CmonBar.swift"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [ "${1:-}" = "uninstall" ]; then
    rm -rf "$APP" "$PLIST"
    echo "cmon Bar removed (settings kept in ~/.cmon/bar.json)"
    exit 0
fi

mkdir -p "$APP/Contents/MacOS" "$(dirname "$PLIST")"
swiftc -O -parse-as-library -o "$APP/Contents/MacOS/CmonBar" "$SRC"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>cmon Bar</string>
  <key>CFBundleExecutable</key><string>CmonBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
codesign --force --sign - "$APP" >/dev/null 2>&1

# KeepAlive only on crash: 'Quit' stays quit until next login.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/CmonBar</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
EOF
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "cmon Bar running · settings: ~/.cmon/bar.json · remove: $0 uninstall"
