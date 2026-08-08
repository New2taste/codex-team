#!/bin/sh
# Install only the ai-workflow-owned Luna template after a full safe preflight.
set -eu

PLUGIN_VERSION=0.2.0
TARGET_FILENAME=luna-worker.toml
STATE_FILENAME=.ai-workflow-luna-worker.state
BACKUP_FILENAME=.ai-workflow-luna-worker.backup
KNOWN_LEGACY_SHA256=60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024
if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x /Users/lee/.local/bin/python3.11 ]; then
        PYTHON_BIN=/Users/lee/.local/bin/python3.11
    elif command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN=python3.11
    else
        PYTHON_BIN=python3
    fi
fi

fail() {
    printf '%s\n' "ai-workflow agent install failed: $1" >&2
    exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python3 is required"
"$PYTHON_BIN" - <<'PY' || fail "Python 3.11+ is required"
import sys
raise SystemExit(sys.version_info < (3, 11))
PY

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || fail "plugin path"
template=$script_dir/../agents/$TARGET_FILENAME

target_input=
check_only=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-dir)
            [ "$#" -ge 2 ] || fail "--target-dir requires a path"
            [ -z "$target_input" ] || fail "--target-dir was supplied more than once"
            target_input=$2
            shift 2
            ;;
        --check)
            check_only=true
            shift
            ;;
        *)
            fail "unsupported argument"
            ;;
    esac
done

if [ -z "$target_input" ]; then
    if [ -n "${CODEX_HOME:-}" ]; then
        target_input=$CODEX_HOME/agents
    else
        target_input=${HOME:?HOME is required when CODEX_HOME is unset}/.codex/agents
    fi
fi

target_dir=$(
    "$PYTHON_BIN" - "$target_input" <<'PY'
import os
import sys

value = sys.argv[1]
if not value:
    raise SystemExit(1)
raw = os.path.abspath(value)
if os.path.islink(raw):
    raise SystemExit(1)
print(os.path.realpath(raw))
PY
) || fail "empty or invalid target directory"
[ "$target_dir" != / ] || fail "refusing filesystem root"

validate_target_directory() {
    "$PYTHON_BIN" - "$target_dir" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1])
if (
    not target.is_absolute()
    or target == Path(target.anchor)
    or target.is_symlink()
    or (target.exists() and not target.is_dir())
):
    raise SystemExit(1)
PY
}

hash_file() {
    "$PYTHON_BIN" - "$1" <<'PY'
import hashlib
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
except OSError:
    raise SystemExit(1)
print(digest.hexdigest())
PY
}

validate_template() {
    [ ! -L "$template" ] && [ -f "$template" ] || return 1
    template_sha=$(hash_file "$template") || return 1
    "$PYTHON_BIN" - "$template" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    value = tomllib.load(handle)
if (
    value.get("name") != "luna_worker"
    or value.get("model") != "gpt-5.6-luna"
    or value.get("model_reasoning_effort") != "max"
    or not isinstance(value.get("developer_instructions"), str)
    or "L0/L1/L2" not in value["developer_instructions"]
):
    raise SystemExit(1)
PY
}

validate_state() {
    "$PYTHON_BIN" - "$state_path" "$template_sha" <<'PY'
import json
import re
import sys

path, expected_sha = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
expected_keys = {
    "plugin_version", "target_filename", "installed_sha256", "installed_at_utc", "backup_sha256"
}
backup = state.get("backup_sha256") if isinstance(state, dict) else None
if (
    not isinstance(state, dict)
    or set(state) != expected_keys
    or state.get("plugin_version") != "0.2.0"
    or state.get("target_filename") != "luna-worker.toml"
    or state.get("installed_sha256") != expected_sha
    or not isinstance(state.get("installed_at_utc"), str)
    or not state["installed_at_utc"]
    or (backup is not None and (not isinstance(backup, str) or not re.fullmatch(r"[0-9a-f]{64}", backup)))
):
    raise SystemExit(1)
print(backup or "")
PY
}

validate_backup() {
    [ -n "$1" ] || return 0
    [ ! -L "$backup_path" ] && [ -f "$backup_path" ] || return 1
    backup_actual=$(hash_file "$backup_path") || return 1
    [ "$backup_actual" = "$1" ]
}

classify() {
    classification=
    state_backup=
    [ -d "$target_dir" ] || [ ! -e "$target_dir" ] || return 1
    [ ! -L "$target_dir" ] || return 1
    destination=$target_dir/$TARGET_FILENAME
    state_path=$target_dir/$STATE_FILENAME
    backup_path=$target_dir/$BACKUP_FILENAME

    if [ -e "$state_path" ] || [ -L "$state_path" ]; then
        [ ! -L "$state_path" ] && [ -f "$state_path" ] || return 1
        state_backup=$(validate_state) || return 1
        [ ! -L "$destination" ] && [ -f "$destination" ] || return 1
        destination_sha=$(hash_file "$destination") || return 1
        [ "$destination_sha" = "$template_sha" ] || return 1
        validate_backup "$state_backup" || return 1
        if [ -z "$state_backup" ] && { [ -e "$backup_path" ] || [ -L "$backup_path" ]; }; then
            return 1
        fi
        classification=current
        return 0
    fi

    if [ -e "$backup_path" ] || [ -L "$backup_path" ]; then
        return 1
    fi
    if [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
        classification=missing
        return 0
    fi
    [ ! -L "$destination" ] && [ -f "$destination" ] || return 1
    destination_sha=$(hash_file "$destination") || return 1
    [ "$destination_sha" = "$KNOWN_LEGACY_SHA256" ] || {
        classification=conflict
        return 0
    }
    classification=known_legacy
}

write_state() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, backup_sha = sys.argv[1:]
state = {
    "plugin_version": "0.2.0",
    "target_filename": "luna-worker.toml",
    "installed_sha256": "60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024",
    "installed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "backup_sha256": backup_sha or None,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, separators=(",", ":"))
    handle.write("\n")
PY
}

validate_target_directory || fail "unsafe target directory"
validate_template || fail "invalid release template"
classify || fail "unsafe or unreadable target"

if [ "$check_only" = true ]; then
    if [ "$classification" = current ]; then
        printf '%s\n' 'ai-workflow agent check: current'
        exit 0
    fi
    printf '%s\n' "ai-workflow agent check: $classification" >&2
    exit 1
fi

if [ "$classification" = current ]; then
    printf '%s\n' 'ai-workflow agent install: current'
    exit 0
fi
[ "$classification" = missing ] || [ "$classification" = known_legacy ] || fail "conflicting target"

if [ "$classification" = missing ]; then
    mkdir -p -- "$target_dir" || fail "cannot create target directory"
fi
validate_target_directory || fail "unsafe target directory"
classify || fail "unsafe or unreadable target"
[ "$classification" = missing ] || [ "$classification" = known_legacy ] || fail "target changed during preflight"

umask 077
agent_temporary=$(mktemp "$target_dir/.ai-workflow-agent.XXXXXX") || fail "cannot stage agent"
state_temporary=$(mktemp "$target_dir/.ai-workflow-state.XXXXXX") || {
    rm -f "$agent_temporary"
    fail "cannot stage state"
}
backup_temporary=
cleanup() {
    rm -f "$agent_temporary" "$state_temporary" ${backup_temporary:+"$backup_temporary"}
}
trap cleanup EXIT HUP INT TERM

cp "$template" "$agent_temporary" || fail "cannot stage agent"
chmod 600 "$agent_temporary" || fail "cannot protect staged agent"
backup_sha=
if [ "$classification" = known_legacy ] && [ "$destination_sha" != "$template_sha" ]; then
    backup_temporary=$(mktemp "$target_dir/.ai-workflow-backup.XXXXXX") || fail "cannot stage backup"
    cp "$destination" "$backup_temporary" || fail "cannot stage backup"
    backup_sha=$(hash_file "$backup_temporary") || fail "cannot hash staged backup"
fi
write_state "$state_temporary" "$backup_sha" || fail "cannot stage state"

validate_target_directory || fail "unsafe target directory"
classify || fail "unsafe or unreadable target"
[ "$classification" = missing ] || [ "$classification" = known_legacy ] || fail "target changed during installation"

if [ -n "$backup_temporary" ]; then
    mv -f "$backup_temporary" "$backup_path" || fail "cannot preserve backup"
    backup_temporary=
fi
mv -f "$agent_temporary" "$destination" || fail "cannot install agent"
agent_temporary=
mv -f "$state_temporary" "$state_path" || fail "cannot record installed state"
state_temporary=
trap - EXIT HUP INT TERM
printf '%s\n' "ai-workflow agent install: $classification"
