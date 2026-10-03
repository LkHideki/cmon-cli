#!/bin/sh
# Compile CmonBar.swift (minus its @main entry point) with Tests.swift and run the checks.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
sed '/^@main/,$d' "$DIR/CmonBar.swift" > "$WORK/main.swift"
cat "$DIR/Tests.swift" >> "$WORK/main.swift"
swiftc -o "$WORK/tests" "$WORK/main.swift"
"$WORK/tests"
