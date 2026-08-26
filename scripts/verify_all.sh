#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
python_bin=${PYTHON_BIN:-python3.11}

cd "$repository_root"
"$python_bin" -m unittest discover -s tests
"$python_bin" -m compileall -q \
    config scripts tests plugins/ai-workflow/runtime plugins/ai-workflow/scripts
"$python_bin" scripts/sync_plugin.py --check
sh plugins/ai-workflow/scripts/verify.sh
for script in scripts/*.sh plugins/ai-workflow/scripts/*.sh
do
    sh -n "$script"
done
git diff --check
# Equivalent to git diff --no-index --check for every untracked file.
git ls-files --others --exclude-standard -z | "$python_bin" -c '
import subprocess
import sys

for raw_path in sys.stdin.buffer.read().split(b"\0"):
    if not raw_path:
        continue
    path = raw_path.decode("utf-8", errors="surrogateescape")
    completed = subprocess.run(
        ["git", "diff", "--no-index", "--check", "--", "/dev/null", path],
        check=False,
        capture_output=True,
    )
    if completed.returncode not in (0, 1) or completed.stdout or completed.stderr:
        sys.stdout.buffer.write(completed.stdout)
        sys.stderr.buffer.write(completed.stderr)
        raise SystemExit(1)
'

printf '%s\n' "ai-workflow full verification: ok"
