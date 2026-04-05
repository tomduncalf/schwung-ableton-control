#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/install_move.sh" && "$SCRIPT_DIR/install_remote_script.sh"
