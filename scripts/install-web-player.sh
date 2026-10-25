#!/bin/zsh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.wassclaw.podcast-manager-web"
PLIST_SOURCE="$REPO_DIR/launchagents/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"

[[ -x "$PYTHON_BIN" ]] || { echo "Python not found: $PYTHON_BIN" >&2; exit 1; }
[[ -f "$PLIST_SOURCE" ]] || { echo "Missing LaunchAgent plist: $PLIST_SOURCE" >&2; exit 1; }

cd "$REPO_DIR"
"$PYTHON_BIN" -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p "$HOME/Library/LaunchAgents" "$REPO_DIR/../../var/log"
plutil -lint "$PLIST_SOURCE"
cp "$PLIST_SOURCE" "$PLIST_DEST"

# Stop an earlier instance if present, then load the repository's canonical plist.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Podcast player installed: http://127.0.0.1:5050"
