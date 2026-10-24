# Shared by scripts/install.sh. Sourced, not executed.

DEFAULT_LABEL_PREFIX="com.podcast-manager"
DEFAULT_TOOL_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

render_plist() {
    local template="$1"
    local dest="$2"
    local project_dir="$3"
    local label="$4"
    local path_value="$5"
    python3 - "$template" "$dest" "$project_dir" "$label" "$path_value" <<'PY'
import pathlib
import sys

template, dest, project_dir, label, path_value = sys.argv[1:]
text = pathlib.Path(template).read_text()
replacements = {
    "__PROJECT_DIR__": project_dir,
    "__LABEL__": label,
    "__PATH__": path_value,
}
for key, value in replacements.items():
    if key not in text:
        raise SystemExit(f"missing {key} in {template}")
    text = text.replace(key, value)
pathlib.Path(dest).write_text(text)
PY
}

valid_label_prefix() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]
}

read_label_prefix() {
    local file="$1"
    local line value
    LAUNCH_LABEL_PREFIX="${DEFAULT_LABEL_PREFIX}"
    if [[ ! -f "${file}" ]]; then
        return
    fi
    while IFS= read -r line || [[ -n "${line}" ]]; do
        [[ "${line}" =~ ^[[:space:]]*# ]] && continue
        if [[ "${line}" =~ ^LAUNCH_LABEL_PREFIX=(.*)$ ]]; then
            value="${BASH_REMATCH[1]}"
            value="${value%\"}"
            value="${value#\"}"
            value="${value%\'}"
            value="${value#\'}"
            LAUNCH_LABEL_PREFIX="${value}"
        fi
    done < "${file}"
}

retire_agents_for_script() {
    local script_name="$1"
    local keep_label="$2"
    local plist label user_id
    user_id="$(id -u)"
    for plist in "${HOME}/Library/LaunchAgents/"*.plist; do
        [[ -e "${plist}" ]] || continue
        if ! grep -F -q "${ROOT}/scripts/${script_name}" "${plist}"; then
            continue
        fi
        label="$(basename "${plist}" .plist)"
        if [[ "${label}" == "${keep_label}" ]]; then
            continue
        fi
        launchctl bootout "gui/${user_id}/${label}" 2>/dev/null || true
        rm -f "${plist}"
        echo "Removed the old launch agent ${label}"
    done
}

write_env_file() {
    local dest="$1"
    local api_key="$2"
    local api_secret="$3"
    python3 - "$dest" "$api_key" "$api_secret" <<'PY'
import pathlib
import sys

dest, api_key, api_secret = sys.argv[1:]
path = pathlib.Path(dest)
path.write_text(f"API_KEY={api_key}\nAPI_SECRET={api_secret}\n")
path.chmod(0o600)
PY
}
