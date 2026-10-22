#!/bin/bash
# Downloads, transcribes, and slices the latest episode of every show.
# Does not need SWIMBUDS. launchd runs this at 4:00 AM local time.

set -uo pipefail

PROJECT_DIR="/Users/ericwasserman/workspace/podcast-manager"
PYTHON="${PROJECT_DIR}/venv/bin/python3"
SCRIPT="${PROJECT_DIR}/podcastsToShokz.py"
LOCK_DIR="/tmp/podcast-nightly-prepare.lock.d"
LOG_FILE="/tmp/podcast-nightly-prepare.log"
SUPPORT_MARKER="${HOME}/Library/Application Support/podcast-manager/podcast_downloader_running.txt"
USB_MARKERS=(
    "/Volumes/SWIMBUDS/podcast_downloader_running.txt"
    "/Volumes/OpenSwim/podcast_downloader_running.txt"
    "/tmp/podcast_downloader_running.txt"
)

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "${LOG_FILE}"
}

cleanup() {
    rmdir "${LOCK_DIR}" 2>/dev/null || true
}

marker_is_active() {
    local path="$1"
    local line pid

    [[ -f "${path}" ]] || return 1
    while IFS= read -r line; do
        if [[ "${line}" == pid=* ]]; then
            pid="${line#pid=}"
            if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
                return 0
            fi
        fi
    done < "${path}"
    rm -f "${path}"
    log "Removed stale running marker: ${path}"
    return 1
}

: > "${LOG_FILE}"
log "Nightly prepare triggered (pid $$, user $(whoami))."

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    log "Another nightly prepare is in progress; skipping."
    exit 0
fi
trap cleanup EXIT

for path in "${SUPPORT_MARKER}" "${USB_MARKERS[@]}"; do
    if marker_is_active "${path}"; then
        log "Updater already running (${path}); skipping."
        exit 0
    fi
done

log "Starting nightly prepare"
cd "${PROJECT_DIR}"

if "${PYTHON}" "${SCRIPT}" --prepare >> "${LOG_FILE}" 2>&1; then
    log "Nightly prepare finished successfully."
else
    status=$?
    log "Nightly prepare failed with exit code ${status}."
    exit "${status}"
fi
