#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_DIR/dist"
MODULE_ID="ableton-control"

echo "Building $MODULE_ID..."

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/$MODULE_ID"

cp "$PROJECT_DIR/src/module.json" "$DIST_DIR/$MODULE_ID/"
cp "$PROJECT_DIR/src/ui.js" "$DIST_DIR/$MODULE_ID/"

cd "$DIST_DIR"
tar -czvf "${MODULE_ID}-module.tar.gz" "$MODULE_ID/"

echo "Built: $DIST_DIR/${MODULE_ID}-module.tar.gz"
