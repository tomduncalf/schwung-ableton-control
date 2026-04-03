#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_DIR/dist"
MODULE_ID="device-control"
MOVE_HOST="${MOVE_HOST:-move.local}"
MOVE_USER="${MOVE_USER:-ableton}"
REMOTE_PATH="/data/UserData/schwung/modules/tools/$MODULE_ID"

# Build first if needed
if [ ! -f "$DIST_DIR/${MODULE_ID}-module.tar.gz" ]; then
    bash "$SCRIPT_DIR/build.sh"
fi

echo "Installing $MODULE_ID to $MOVE_USER@$MOVE_HOST..."

# Create remote directory and copy files
ssh "$MOVE_USER@$MOVE_HOST" "mkdir -p $REMOTE_PATH"
scp "$DIST_DIR/$MODULE_ID/module.json" "$MOVE_USER@$MOVE_HOST:$REMOTE_PATH/"
scp "$DIST_DIR/$MODULE_ID/ui.js" "$MOVE_USER@$MOVE_HOST:$REMOTE_PATH/"

echo "Installed to $REMOTE_PATH"
echo ""
echo "To install the Ableton Remote Script, copy SchwungDeviceControl/ to:"
echo "  ~/Music/Ableton/User Library/Remote Scripts/SchwungDeviceControl/"
echo "Then restart Ableton and add it as a control surface."
