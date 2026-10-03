#!/bin/sh
# Build cmon Bar and start it at login (macOS). Usage: menubar/install.sh [uninstall]
# Needs the Xcode Command Line Tools (swiftc) and a cmon with 'status --json'.
set -eu

LABEL="com.cmon.bar"
APP="$HOME/Applications/cmon Bar.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/CmonBar.swift"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "uninstall" ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -rf "$APP" "$PLIST"
    echo "cmon Bar removed (settings kept in ~/.cmon/bar.json)"
    exit 0
fi

command -v swiftc >/dev/null || { echo "swiftc not found: xcode-select --install" >&2; exit 1; }

# Build the whole bundle aside first: a failed compile must leave the running app untouched.
BUILD="$(mktemp -d)/cmon Bar.app"
trap 'rm -rf "$(dirname "$BUILD")"' EXIT
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources" "$(dirname "$PLIST")" "$(dirname "$APP")"
swiftc -O -parse-as-library -o "$BUILD/Contents/MacOS/CmonBar" "$SRC"
swift "$DIR/make_icon.swift" "$(dirname "$BUILD")/AppIcon.iconset"
iconutil -c icns "$(dirname "$BUILD")/AppIcon.iconset" -o "$BUILD/Contents/Resources/AppIcon.icns"

cat > "$BUILD/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>cmon Bar</string>
  <key>CFBundleExecutable</key><string>CmonBar</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
codesign --force --sign - "$BUILD"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -rf "$APP"
mv "$BUILD" "$APP"

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
# bootout is asynchronous; a bootstrap racing it fails with error 5, so retry briefly.
for _ in 1 2 3 4 5; do
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
    sleep 1
done
launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || { echo "launchctl bootstrap failed" >&2; exit 1; }
echo "cmon Bar running · settings: ~/.cmon/bar.json · remove: $0 uninstall"
