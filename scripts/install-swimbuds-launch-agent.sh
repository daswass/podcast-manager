#!/bin/bash
# Install or reload the SWIMBUDS mount watcher and the 4am prepare job.

set -euo pipefail

PROJECT_DIR="/Users/ericwasserman/workspace/podcast-manager"
USER_ID="$(id -u)"

install_agent() {
    local label="$1"
    local kickstart="$2"
    local plist_src="${PROJECT_DIR}/launchd/${label}.plist"
    local plist_dest="${HOME}/Library/LaunchAgents/${label}.plist"

    cp "${plist_src}" "${plist_dest}"
    launchctl bootout "gui/${USER_ID}/${label}" 2>/dev/null || true
    launchctl bootstrap "gui/${USER_ID}" "${plist_dest}"
    launchctl enable "gui/${USER_ID}/${label}"
    if [[ "${kickstart}" == "yes" ]]; then
        launchctl kickstart -k "gui/${USER_ID}/${label}" 2>/dev/null || true
    fi
    echo "Installed ${label}"
    echo "  plist: ${plist_dest}"
}

chmod +x "${PROJECT_DIR}/scripts/run-on-swimbuds-mount.sh"
chmod +x "${PROJECT_DIR}/scripts/run-nightly-prepare.sh"
mkdir -p "${HOME}/Library/LaunchAgents"

install_agent "com.ericwasserman.podcast-swimbuds-updater" "yes"
install_agent "com.ericwasserman.podcast-nightly-prepare" "no"

echo ""
echo "Logs:"
echo "  /tmp/podcast-swimbuds-updater.log"
echo "  /tmp/podcast-nightly-prepare.log"
echo "Timings:"
echo "  ${HOME}/Library/Application Support/podcast-manager/timing.jsonl"
echo ""
echo "IMPORTANT: For automatic runs on USB mount, grant Full Disk Access to:"
echo "  /bin/bash"
echo "System Settings > Privacy & Security > Full Disk Access > +"
echo ""
echo "The 4am prepare job runs the next time the Mac is awake at 4:00 AM."
echo "Test manually:"
echo "  ${PROJECT_DIR}/scripts/run-nightly-prepare.sh"
echo "  ${PROJECT_DIR}/scripts/run-on-swimbuds-mount.sh"
