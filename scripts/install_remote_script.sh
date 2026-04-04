#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$PROJECT_DIR/ableton_remote_script"
DEST="$HOME/Production/Ableton/User Library/Remote Scripts/SchwungDeviceControl"

# Preserve bindings.json if it exists
BINDINGS=""
if [ -f "$DEST/bindings.json" ]; then
    BINDINGS=$(cat "$DEST/bindings.json")
    echo "Preserving existing bindings.json"
fi

# Replace remote script
rm -rf "$DEST"
cp -R "$SRC" "$DEST"

# Restore bindings
if [ -n "$BINDINGS" ]; then
    echo "$BINDINGS" > "$DEST/bindings.json"
fi

echo "Installed remote script to $DEST"
echo "Restart Ableton to apply changes."
