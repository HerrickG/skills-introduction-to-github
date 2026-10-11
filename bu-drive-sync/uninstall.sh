#!/usr/bin/env bash
# Stop and remove the bu-drive-sync login agent. Nothing in Google Drive is touched.
#
#   ./uninstall.sh          # stop the agent, keep the installed files and log
#   ./uninstall.sh --purge  # also delete the installed files and log
set -euo pipefail

LABEL="local.bu-drive-sync"
APP_DIR="$HOME/Library/Application Support/bu-drive-sync"
LOG="$HOME/Library/Logs/bu-drive-sync.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2> /dev/null || true
rm -f "$PLIST"
echo "Agent stopped and removed."

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$APP_DIR" "$LOG"
    echo "Deleted $APP_DIR and $LOG."
else
    echo "Installed files kept at: $APP_DIR"
    echo "Log kept at:             $LOG"
    echo "Run ./uninstall.sh --purge to delete them too."
fi
