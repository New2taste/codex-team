#!/bin/sh
# POSIX entrypoint for cleanup-only historical Agent removal.
set -eu

if [ -z "${PYTHON_BIN:-}" ]; then
    if command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN=python3.11
    else
        PYTHON_BIN=python3
    fi
fi

fail() {
    printf '%s\n' "ai-workflow agent uninstall failed: $1" >&2
    exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3.11+ is required"
"$PYTHON_BIN" - <<'PY' || fail "Python 3.11+ is required"
import sys
raise SystemExit(sys.version_info < (3, 11))
PY

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || fail "plugin path"
exec "$PYTHON_BIN" "$script_dir/agent_lifecycle.py" uninstall "$@"
