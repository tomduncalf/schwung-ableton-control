#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$PROJECT_DIR/ableton_remote_script"
DEST="$HOME/Production/Ableton/User Library/Remote Scripts/SchwungDeviceControl"

# Replace remote script (bindings are stored in ~/Library/Application Support/SchwungDeviceControl/)
rm -rf "$DEST"
cp -R "$SRC" "$DEST"

echo "Installed remote script to $DEST"
echo "Restart Ableton to apply changes."
