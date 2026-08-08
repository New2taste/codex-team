#!/bin/sh
# Remove only an unchanged companion Agent that has a valid ai-workflow state record.
set -eu

TARGET_FILENAME=luna-worker.toml
STATE_FILENAME=.ai-workflow-luna-worker.state
BACKUP_FILENAME=.ai-workflow-luna-worker.backup
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
    printf '%s\n' "ai-workflow agent uninstall failed: $1" >&2
    exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python3 is required"
"$PYTHON_BIN" - <<'PY' || fail "Python 3.11+ is required"
import sys
raise SystemExit(sys.version_info < (3, 11))
PY

target_input=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-dir)
            [ "$#" -ge 2 ] || fail "--target-dir requires a path"
            [ -z "$target_input" ] || fail "--target-dir was supplied more than once"
            target_input=$2
            shift 2
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
    or not target.is_dir()
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
try:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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

state_backup() {
    "$PYTHON_BIN" - "$state_path" "$1" <<'PY'
import json
import re
import sys
path, current_sha = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
keys = {"plugin_version", "target_filename", "installed_sha256", "installed_at_utc", "backup_sha256"}
backup = state.get("backup_sha256") if isinstance(state, dict) else None
if (
    not isinstance(state, dict)
    or set(state) != keys
    or state.get("plugin_version") != "0.2.0"
    or state.get("target_filename") != "luna-worker.toml"
    or state.get("installed_sha256") != current_sha
    or not isinstance(state.get("installed_at_utc"), str)
    or not state["installed_at_utc"]
    or (backup is not None and (not isinstance(backup, str) or not re.fullmatch(r"[0-9a-f]{64}", backup)))
):
    raise SystemExit(1)
print(backup or "")
PY
}

validate_target_directory || fail "unsafe target directory"
destination=$target_dir/$TARGET_FILENAME
state_path=$target_dir/$STATE_FILENAME
backup_path=$target_dir/$BACKUP_FILENAME
[ ! -L "$destination" ] && [ -f "$destination" ] || fail "agent is missing or unsafe"
[ ! -L "$state_path" ] && [ -f "$state_path" ] || fail "state is missing or unsafe"
installed_sha=$(hash_file "$destination") || fail "agent is unreadable"
backup_sha=$(state_backup "$installed_sha") || fail "agent is modified or state is invalid"

if [ -n "$backup_sha" ]; then
    [ ! -L "$backup_path" ] && [ -f "$backup_path" ] || fail "backup is missing or unsafe"
    actual_backup_sha=$(hash_file "$backup_path") || fail "backup is unreadable"
    [ "$actual_backup_sha" = "$backup_sha" ] || fail "backup is modified"
    umask 077
    restore_temporary=$(mktemp "$target_dir/.ai-workflow-restore.XXXXXX") || fail "cannot stage backup restore"
    cleanup() { rm -f "$restore_temporary"; }
    trap cleanup EXIT HUP INT TERM
    cp "$backup_path" "$restore_temporary" || fail "cannot stage backup restore"
    [ "$(hash_file "$restore_temporary")" = "$backup_sha" ] || fail "backup changed during restore"
    validate_target_directory || fail "unsafe target directory"
    current_sha=$(hash_file "$destination") || fail "agent changed during uninstall"
    [ "$current_sha" = "$installed_sha" ] || fail "agent changed during uninstall"
    [ "$(state_backup "$current_sha")" = "$backup_sha" ] || fail "state changed during uninstall"
    mv -f "$restore_temporary" "$destination" || fail "cannot restore backup"
    restore_temporary=
    rm -f "$backup_path" "$state_path" || fail "cannot remove owned state"
    trap - EXIT HUP INT TERM
    printf '%s\n' 'ai-workflow agent uninstall: restored backup'
    exit 0
fi

if [ -e "$backup_path" ] || [ -L "$backup_path" ]; then
    fail "unexpected backup without state ownership"
fi
validate_target_directory || fail "unsafe target directory"
current_sha=$(hash_file "$destination") || fail "agent changed during uninstall"
[ "$current_sha" = "$installed_sha" ] || fail "agent changed during uninstall"
[ -z "$(state_backup "$current_sha")" ] || fail "state changed during uninstall"
rm -f "$destination" "$state_path" || fail "cannot remove owned agent"
printf '%s\n' 'ai-workflow agent uninstall: removed'
