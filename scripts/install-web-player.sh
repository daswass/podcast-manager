#!/bin/bash
# Install the local player as a per-user LaunchAgent for this checkout.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=install-lib.sh
source "${ROOT}/scripts/install-lib.sh"

cd "${ROOT}"
read_label_prefix "${ROOT}/local.env"
if ! valid_label_prefix "${LAUNCH_LABEL_PREFIX}"; then
    echo "LAUNCH_LABEL_PREFIX in local.env must look like com.podcast-manager" >&2
    exit 1
fi

label="${LAUNCH_LABEL_PREFIX}.web-player"
archive="${HOME}/Library/Application Support/podcast-manager"
template="${ROOT}/launchd/web-player.plist.template"
dest="${HOME}/Library/LaunchAgents/${label}.plist"
user_id="$(id -u)"

if [[ ! -x "${ROOT}/venv/bin/python" ]]; then
    python3 -m venv "${ROOT}/venv"
fi
"${ROOT}/venv/bin/pip" install -r "${ROOT}/requirements.txt"

mkdir -p "${HOME}/Library/LaunchAgents" "${archive}"
render_plist "${template}" "${dest}" "${ROOT}" "${label}" "${DEFAULT_TOOL_PATH}"
python3 - "${dest}" "${archive}" <<'PY'
import pathlib
import sys

dest, archive = sys.argv[1:]
path = pathlib.Path(dest)
text = path.read_text()
needle = "__ARCHIVE_DIR__"
if needle not in text:
    raise SystemExit(f"missing {needle} in {dest}")
path.write_text(text.replace(needle, archive))
PY
plutil -lint "${dest}"

# The other checkout shipped a plist whose paths and label belong to that machine.
old_label="com.wassclaw.podcast-manager-web"
launchctl bootout "gui/${user_id}/${old_label}" 2>/dev/null || true
rm -f "${HOME}/Library/LaunchAgents/${old_label}.plist"

launchctl bootout "gui/${user_id}/${label}" 2>/dev/null || true
launchctl bootstrap "gui/${user_id}" "${dest}"
launchctl enable "gui/${user_id}/${label}"
launchctl kickstart -k "gui/${user_id}/${label}"

echo "Installed ${label}"
echo "  ${dest}"
echo "Local player: http://127.0.0.1:5050"
echo "Log: /tmp/podcast-manager-player.out"
