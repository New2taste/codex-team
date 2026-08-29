"""Capture a pass/skip test baseline manifest from the current checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "ai-test-baseline-1"
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ROOT = Path(__file__).resolve().parents[1]


class BaselineTestResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.entries: list[dict[str, str]] = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.entries.append(
            {"id": test.id(), "outcome": "pass", "skip_reason": ""}
        )

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        skip_reason = str(reason)
        self.entries.append(
            {"id": test.id(), "outcome": "skip", "skip_reason": skip_reason}
        )


class CollectionError(RuntimeError):
    pass


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise CollectionError(f"git rev-parse HEAD failed: {detail}")
    sha = completed.stdout.strip()
    if COMMIT_SHA.fullmatch(sha) is None:
        raise CollectionError(f"HEAD is not a 40-character hex commit: {sha!r}")
    return sha


def _captured_with(argv: list[str] | None) -> str:
    program = sys.argv[0]
    if argv is None:
        command = [sys.executable, *sys.argv]
    else:
        command = [sys.executable, program, *argv]
    return shlex.join(command)


def _discover(root: Path) -> unittest.TestSuite:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    start_dir = str(root / "tests")
    return unittest.defaultTestLoader.discover(
        start_dir=start_dir,
        pattern="test*.py",
    )


def _run_suite(suite: unittest.TestSuite) -> BaselineTestResult:
    runner = unittest.TextTestRunner(
        stream=sys.stderr,
        verbosity=2,
        resultclass=BaselineTestResult,
    )
    result = runner.run(suite)
    if not isinstance(result, BaselineTestResult):
        raise CollectionError("unittest runner did not return BaselineTestResult")
    return result


def _fail_if_red(result: BaselineTestResult) -> None:
    if result.wasSuccessful() and not result.errors and not result.failures:
        return
    lines = ["BASELINE_COLLECTION_FAILED: tests did not all pass or skip"]
    for test, traceback in result.failures:
        lines.append(f"FAIL {test.id()}\n{traceback}")
    for test, traceback in result.errors:
        lines.append(f"ERROR {test.id()}\n{traceback}")
    raise CollectionError("\n".join(lines))


def _sorted_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise CollectionError("duplicate test ids in collection result")
    for entry in entries:
        outcome = entry["outcome"]
        reason = entry["skip_reason"]
        if outcome not in {"pass", "skip"}:
            raise CollectionError(f"unsupported outcome for {entry['id']}: {outcome}")
        if outcome == "skip":
            if not reason:
                raise CollectionError(f"skip_reason must be nonempty for {entry['id']}")
        elif reason:
            raise CollectionError(f"skip_reason must be empty for pass {entry['id']}")
    return sorted(entries, key=lambda entry: entry["id"])


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def collect(root: Path, output: Path, *, captured_with: str) -> dict:
    os.chdir(root)
    suite = _discover(root)
    result = _run_suite(suite)
    _fail_if_red(result)
    expected = suite.countTestCases()
    if len(result.entries) != expected:
        raise CollectionError(
            f"recorded {len(result.entries)} results but suite has {expected} tests"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "base_commit": _git_head(root),
        "captured_with": captured_with,
        "captured_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "tests": _sorted_entries(result.entries),
    }
    _write_manifest(output, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collect-test-baseline")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output
    if not output.is_absolute():
        output = (ROOT / output).resolve()
    try:
        collect(ROOT, output, captured_with=_captured_with(argv))
    except CollectionError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
