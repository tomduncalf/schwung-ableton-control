#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/build.sh" && "$SCRIPT_DIR/install.sh" && ssh root@move.local "/etc/init.d/move stop && /etc/init.d/move start"
