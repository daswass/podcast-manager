#!/bin/bash
# Install or reload the SWIMBUDS mount watcher LaunchAgent.

set -euo pipefail

LABEL="com.ericwasserman.podcast-swimbuds-updater"
PROJECT_DIR="/Users/ericwasserman/workspace/podcast-manager"
PLIST_SRC="${PROJECT_DIR}/launchd/${LABEL}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
WRAPPER="${PROJECT_DIR}/scripts/run-on-swimbuds-mount.sh"

chmod +x "${WRAPPER}"

mkdir -p "${HOME}/Library/LaunchAgents"
cp "${PLIST_SRC}" "${PLIST_DEST}"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true

echo "Installed ${LABEL}"
echo "  plist:  ${PLIST_DEST}"
echo "  logs:   /tmp/podcast-swimbuds-updater.log"
echo "  stderr: /tmp/podcast-swimbuds-launchd.err"
echo ""
echo "IMPORTANT: For automatic runs on USB mount, grant Full Disk Access to:"
echo "  /bin/bash"
echo "System Settings > Privacy & Security > Full Disk Access > +"
echo ""
echo "Test manually:"
echo "  ${WRAPPER}"
