#!/bin/bash
# Runs podcastsToShokz.py when /Volumes/SWIMBUDS is mounted.
# Invoked by launchd on volume mount (WatchPaths) or daily at 6:40 AM.

set -uo pipefail

VOLUME="/Volumes/SWIMBUDS"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PROJECT_DIR}/venv/bin/python3"
SCRIPT="${PROJECT_DIR}/podcastsToShokz.py"
LOCK_DIR="/tmp/podcast-swimbuds-updater.lock.d"
LOG_FILE="/tmp/podcast-swimbuds-updater.log"
RUNNING_MARKER="${VOLUME}/podcast_downloader_running.txt"
TMP_RUNNING_MARKER="/tmp/podcast_downloader_running.txt"
SUPPORT_MARKER="${HOME}/Library/Application Support/podcast-manager/podcast_downloader_running.txt"
NEEDS_COPY="${HOME}/Library/Application Support/podcast-manager/needs_copy"
MIN_INTERVAL_SECONDS=$((3 * 60 * 60))

# launchd only provides /usr/bin:/bin:/usr/sbin:/sbin — ffmpeg lives in Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "${LOG_FILE}"
}

cleanup() {
    rmdir "${LOCK_DIR}" 2>/dev/null || true
}

read_timestamp_file() {
    local path="$1"
    local value=""

    if [[ ! -f "${path}" ]]; then
        return 1
    fi

    if value="$(cat "${path}" 2>/dev/null)" && [[ "${value}" =~ ^[0-9]+$ ]]; then
        echo "${value}"
        return 0
    fi

    return 1
}

running_marker_pid() {
    local path="$1"
    local line pid

    [[ -f "${path}" ]] || return 1
    while IFS= read -r line; do
        if [[ "${line}" == pid=* ]]; then
            pid="${line#pid=}"
            if [[ "${pid}" =~ ^[0-9]+$ ]]; then
                echo "${pid}"
                return 0
            fi
        fi
    done < "${path}"
    return 1
}

running_marker_is_active() {
    local path="$1"
    local pid

    [[ -f "${path}" ]] || return 1
    pid="$(running_marker_pid "${path}")" || return 1
    kill -0 "${pid}" 2>/dev/null
}

clear_stale_running_marker() {
    local path="$1"

    if [[ ! -f "${path}" ]]; then
        return 0
    fi

    if running_marker_is_active "${path}"; then
        return 1
    fi

    rm -f "${path}"
    log "Removed stale running marker: ${path}"
    return 0
}

check_running_marker() {
    local path

    for path in "${RUNNING_MARKER}" "${TMP_RUNNING_MARKER}" "${SUPPORT_MARKER}"; do
        if [[ ! -f "${path}" ]]; then
            continue
        fi

        if running_marker_is_active "${path}"; then
            log "Updater already running (${path} present); skipping."
            return 1
        fi

        clear_stale_running_marker "${path}"
    done

    return 0
}

: > "${LOG_FILE}"
log "Watcher triggered (pid $$, user $(whoami))."

if [[ ! -d "${VOLUME}" ]]; then
    log "SWIMBUDS not mounted; skipping."
    exit 0
fi

log "SWIMBUDS detected; waiting for mount to settle."
sleep 5

if [[ ! -d "${VOLUME}" ]]; then
    log "Volume disappeared during mount wait; skipping."
    exit 0
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    log "Another updater run is in progress; skipping."
    exit 0
fi
trap cleanup EXIT

if ! check_running_marker; then
    exit 0
fi

timestamp_file="${VOLUME}/podcast_downloader_last_run.txt"
fallback_timestamp_file="/tmp/podcast_downloader_last_run.txt"
last_run=0

if last_run="$(read_timestamp_file "${timestamp_file}")"; then
    log "Last run timestamp from drive: ${last_run} ($(date -r "${last_run}" 2>/dev/null || echo unknown))."
elif last_run="$(read_timestamp_file "${fallback_timestamp_file}")"; then
    log "Could not read drive timestamp; using fallback ${fallback_timestamp_file}: ${last_run}."
else
    last_run=0
    log "No readable timestamp file; treating as first run."
fi

now="$(date +%s)"
if (( last_run > now - MIN_INTERVAL_SECONDS )); then
    if [[ -f "${NEEDS_COPY}" ]]; then
        log "Sliced episodes are waiting to copy; running despite the recent sync."
    else
        log "Skip run. Updated within the last 3 hours (last run epoch: ${last_run})."
        exit 0
    fi
fi

log "Starting podcast updater for ${VOLUME}"
log "PATH=${PATH}"
log "ffmpeg=$(command -v ffmpeg || echo not found)"
cd "${PROJECT_DIR}"

if "${PYTHON}" "${SCRIPT}" >> "${LOG_FILE}" 2>&1; then
    log "Podcast updater finished successfully."
else
    status=$?
    log "Podcast updater failed with exit code ${status}."
    if grep -q "Operation not permitted" "${LOG_FILE}" 2>/dev/null; then
        log "Hint: grant Full Disk Access to /bin/bash in System Settings > Privacy & Security > Full Disk Access, then replug the drive or rerun the install script."
    fi
    exit "${status}"
fi
