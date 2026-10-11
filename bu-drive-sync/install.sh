#!/usr/bin/env bash
# Install bu-drive-sync as a macOS login agent (launchd).
#
#   ./install.sh                      # watch ~/Documents/Development/BU -> <Google Drive>/My Drive/BU
#   ./install.sh --mirror-deletes     # extra flags are passed through to bu_drive_sync.py
#   BU_SOURCE=/other/folder ./install.sh
#   BU_DEST="/path/to/My Drive/BU" ./install.sh
#
# Re-run at any time to update the script or change options.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="local.bu-drive-sync"
SOURCE="${BU_SOURCE:-$HOME/Documents/Development/BU}"
DEST="${BU_DEST:-}"
APP_DIR="$HOME/Library/Application Support/bu-drive-sync"
VENV="$APP_DIR/venv"
LOG="$HOME/Library/Logs/bu-drive-sync.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
EXTRA_ARGS=("$@")

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This installer is for macOS (it uses launchd)." >&2
    exit 1
fi
if [[ ! -d "$SOURCE" ]]; then
    echo "Source folder not found: $SOURCE" >&2
    echo "Set BU_SOURCE=/path/to/folder to use a different one." >&2
    exit 1
fi
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    echo "python3 not found. Install the Xcode Command Line Tools first:  xcode-select --install" >&2
    exit 1
fi

echo "==> Setting up $APP_DIR"
mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$HERE/requirements.txt"
cp "$HERE/bu_drive_sync.py" "$APP_DIR/bu_drive_sync.py"

if [[ -z "$DEST" ]]; then
    if ! DEST="$("$VENV/bin/python" "$APP_DIR/bu_drive_sync.py" --find-dest)"; then
        echo "Could not find your Google Drive folder." >&2
        echo "Install Google Drive for Desktop and sign in, or run:" >&2
        echo "  BU_DEST=\"\$HOME/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/BU\" ./install.sh" >&2
        exit 1
    fi
fi
echo "    Source: $SOURCE"
echo "    Dest:   $DEST"

# One pass in the foreground first: you see the initial copy happen, and macOS
# shows its folder-access prompts here in the Terminal rather than silently
# blocking the background agent.
echo "==> Initial sync"
"$VENV/bin/python" "$APP_DIR/bu_drive_sync.py" --source "$SOURCE" --dest "$DEST" --once \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

echo "==> Writing $PLIST"
{
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "$VENV/bin/python")</string>
        <string>$(xml_escape "$APP_DIR/bu_drive_sync.py")</string>
        <string>--source</string>
        <string>$(xml_escape "$SOURCE")</string>
        <string>--dest</string>
        <string>$(xml_escape "$DEST")</string>
EOF
    for arg in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
        printf '        <string>%s</string>\n' "$(xml_escape "$arg")"
    done
    cat <<EOF
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$LOG")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
} > "$PLIST"
plutil -lint "$PLIST" > /dev/null

echo "==> Starting the background agent"
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2> /dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
sleep 2
if launchctl print "gui/$UID_NUM/$LABEL" > /dev/null 2>&1; then
    echo "    Running. It starts automatically at every login."
else
    echo "    The agent did not start. Check the log: $LOG" >&2
    exit 1
fi

REAL_PYTHON="$("$VENV/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
cat <<EOF

Done.
  Log:            $LOG
  Watch it live:  tail -f "$LOG"
  Stop/remove:    $HERE/uninstall.sh

If the log shows "Operation not permitted", macOS is blocking the background
process from reading your Documents folder. Open System Settings > Privacy &
Security > Full Disk Access and add this program:
  $REAL_PYTHON
EOF
