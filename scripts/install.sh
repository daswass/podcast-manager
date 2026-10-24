#!/bin/bash
# Install the Python environment, the web page, and optional login jobs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=install-lib.sh
source "${ROOT}/scripts/install-lib.sh"

cd "${ROOT}"

if [[ ! -t 0 ]]; then
    echo "Run ./scripts/install.sh in a terminal. It asks before installing login jobs." >&2
    exit 1
fi

require_command() {
    local name="$1"
    if ! command -v "${name}" >/dev/null 2>&1; then
        echo "Missing ${name}." >&2
        exit 1
    fi
}

confirm() {
    local prompt="$1"
    local reply
    read -r -p "${prompt} [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]]
}

ask_secret() {
    local prompt="$1"
    local reply
    read -r -s -p "${prompt}" reply
    echo "" >&2
    if [[ "${reply}" == *$'\n'* ]]; then
        echo "That value cannot contain a newline." >&2
        exit 1
    fi
    printf '%s' "${reply}"
}

require_command python3
require_command npm

install_jobs="no"
install_manager="no"
if confirm "Install the headphone copy watcher and the 4:00 AM prepare job?"; then
    install_jobs="yes"
fi
if confirm "Start the manager website at login?"; then
    install_manager="yes"
fi

if [[ ! -f .env ]]; then
    echo "Podcast Index keys go in .env. Create them at https://podcastindex.org/"
    api_key="$(ask_secret "API key: ")"
    api_secret="$(ask_secret "API secret: ")"
    write_env_file "${ROOT}/.env" "${api_key}" "${api_secret}"
    echo "Wrote .env"
else
    echo "Keeping the existing .env"
fi

if [[ ! -f local.env ]]; then
    cp local.env.example local.env
    echo "Wrote local.env from local.env.example"
fi
read_label_prefix "${ROOT}/local.env"
if ! valid_label_prefix "${LAUNCH_LABEL_PREFIX}"; then
    echo "LAUNCH_LABEL_PREFIX in local.env must look like com.podcast-manager" >&2
    exit 1
fi

echo "Creating the Python environment..."
if [[ ! -d venv ]]; then
    python3 -m venv venv
fi
./venv/bin/pip install -r requirements.txt

echo "Building the web page..."
npm --prefix web ci
npm --prefix web run build

chmod +x scripts/run-on-swimbuds-mount.sh scripts/run-nightly-prepare.sh scripts/run-manager.sh scripts/run-manager-service.sh

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg is not on PATH. Trimming audio needs it."
    if command -v brew >/dev/null 2>&1 && confirm "Install ffmpeg with Homebrew?"; then
        brew install ffmpeg
    else
        echo "Install ffmpeg, then run a prepare or copy job."
    fi
fi

install_agent() {
    local template="$1"
    local label="$2"
    local script_name="$3"
    local dest="${HOME}/Library/LaunchAgents/${label}.plist"
    retire_agents_for_script "${script_name}" "${label}"
    render_plist "${template}" "${dest}" "${ROOT}" "${label}" "${DEFAULT_TOOL_PATH}"
    local user_id
    user_id="$(id -u)"
    launchctl bootout "gui/${user_id}/${label}" 2>/dev/null || true
    launchctl bootstrap "gui/${user_id}" "${dest}"
    launchctl enable "gui/${user_id}/${label}"
    echo "Installed ${label}"
    echo "  ${dest}"
}

if [[ "${install_jobs}" == "yes" || "${install_manager}" == "yes" ]]; then
    mkdir -p "${HOME}/Library/LaunchAgents"
fi

if [[ "${install_jobs}" == "yes" ]]; then
    install_agent "${ROOT}/launchd/headphone-copy.plist.template" "${LAUNCH_LABEL_PREFIX}.headphone-copy" "run-on-swimbuds-mount.sh"
    install_agent "${ROOT}/launchd/nightly-prepare.plist.template" "${LAUNCH_LABEL_PREFIX}.nightly-prepare" "run-nightly-prepare.sh"
    echo ""
    echo "The copy watcher needs Full Disk Access for /bin/bash"
    echo "(System Settings → Privacy & Security → Full Disk Access)."
    echo "The 4:00 AM prepare job runs the next time this Mac is awake then."
    echo "Logs: /tmp/podcast-swimbuds-updater.log and /tmp/podcast-nightly-prepare.log"
fi

if [[ "${install_manager}" == "yes" ]]; then
    install_agent "${ROOT}/launchd/manager.plist.template" "${LAUNCH_LABEL_PREFIX}.manager" "run-manager-service.sh"
    user_id="$(id -u)"
    if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Port 8765 is already in use. The website agent will start after that process stops, or at the next login."
    else
        launchctl kickstart "gui/${user_id}/${LAUNCH_LABEL_PREFIX}.manager" 2>/dev/null || true
    fi
    echo "Manager website: http://127.0.0.1:8765"
    echo "Log: /tmp/podcast-manager-web.out"
fi

echo ""
echo "Open the page any time with ./scripts/run-manager.sh"
