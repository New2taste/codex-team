"""Deterministic validation and state transitions for the local workflow stage."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import errno
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence


TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "task_type",
        "objective",
        "repository_root",
        "source_worktree",
        "base_commit",
        "candidate_commit",
        "authoritative_files",
        "allowed_write_paths",
        "forbidden_actions",
        "risk_flags",
        "acceptance_commands",
        "verification_level",
        "human_gates",
    }
)
TASK_TYPES = frozenset({"PLAN", "ACCEPTANCE", "REMEDIATION"})
VERIFICATION_LEVELS = frozenset({"L0", "L1", "L2"})
RISK_FLAGS = frozenset(
    {
        "CONSTITUTION",
        "PIT",
        "SURVIVORSHIP_BIAS",
        "PUBLIC_SCHEMA",
        "APPEND_ONLY",
        "SECURITY",
        "DATA_CONTAMINATION",
        "PUBLIC_API",
        "CROSS_CARD_CONTRACT",
    }
)
HUMAN_GATES = frozenset(
    {
        "PLAN_APPROVAL",
        "EXECUTION_APPROVAL",
        "FINAL_ACCEPTANCE",
        "XHIGH_APPROVAL",
        "MERGE",
        "PUSH",
    }
)
TASK_ID_PATTERN = re.compile(r"^AWF-[0-9]{8}-[0-9]{3,}$")
ROLE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ai_workflow.toml"
RESULT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "role",
        "status",
        "summary",
        "claims",
        "evidence",
        "counter_checks",
        "changed_files",
        "blind_spots",
        "unresolved_questions",
        "recommended_next_state",
    }
)
READ_ONLY_ROLES = frozenset({"luna", "sol_planner", "sol_reviewer", "sol_xhigh"})
ROLE_GUARD_FAILURES = frozenset(
    {
        "ACCEPTANCE_CANDIDATE_HEAD_MISMATCH",
        "ACCEPTANCE_COMMIT_UNRESOLVED",
        "DIRTY_ACCEPTANCE_REPOSITORY",
        "DIRTY_READ_ONLY_REPOSITORY",
        "DIRTY_TERRA_WORKTREE",
        "HEAD_DRIFT",
        "OUT_OF_SCOPE_CHANGE",
        "READ_ONLY_ROLE_MODIFIED_REPO",
        "ROLE_REPOSITORY_MISMATCH",
        "SOURCE_WORKTREE_REQUIRED",
        "TERRA_STATE_NOT_AUTHORIZED",
        "UNAUTHORIZED_SOURCE_WORKTREE",
        "WORKFLOW_STORE_REQUIRED",
    }
)
SAFE_ENVIRONMENT_KEYS = frozenset({"HOME", "PATH", "CODEX_HOME", "LANG", "LC_ALL", "TERM", "TMPDIR"})
CODEX_TIMEOUT_SECONDS = 120
WORKFLOW_STATE_ROOT = Path(__file__).resolve().parents[1] / "data" / "state" / "ai-workflow"
OWNER_DECISIONS = frozenset(
    {
        "approve_execution",
        "authorize_rework",
        "authorize_escalation",
        "defer",
        "close",
        "abort",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:TOKEN|KEY|PASSWORD|SECRET)[A-Z0-9_]*)\"?\s*(?:=|:)\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_LONG_HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9+/=_-]{48,}(?![A-Za-z0-9_])")
METRICS_SCHEMA_VERSION = "ai-metrics-1"


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def route(task: Mapping[str, object]) -> tuple[str, ...]:
    """Return the deterministic, bounded role order for a task summary."""

    if not isinstance(task, Mapping):
        _fail("INVALID_TASK", "task must be an object")
    task_type = task.get("task_type")
    risk_flags = task.get("risk_flags")
    if not isinstance(task_type, str) or task_type not in TASK_TYPES:
        _fail("INVALID_ENUM", "task_type is not supported")
    if isinstance(risk_flags, str) or not isinstance(risk_flags, Sequence):
        _fail("INVALID_TYPE", "risk_flags must be an array")
    if any(not isinstance(flag, str) or flag not in RISK_FLAGS for flag in risk_flags):
        _fail("INVALID_ENUM", "risk_flags contains an unsupported value")
    if task_type == "PLAN":
        return ("luna", "sol_planner")
    if task_type == "ACCEPTANCE":
        return ("luna", "sol_reviewer")
    if risk_flags:
        return ("sol_planner", "terra", "luna", "sol_reviewer")
    return ("terra", "luna", "sol_reviewer")


@dataclass(frozen=True)
class RepoSnapshot:
    """The repository state that a bounded workflow operation is pinned to."""

    head: str
    status: tuple[str, ...]


def git(repo: Path, *args: str) -> str:
    """Run one fixed-form Git command in ``repo`` without a shell."""

    completed = subprocess.run(
        ["git", "-C", str(Path(repo)), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if completed.returncode != 0:
        raise WorkflowError("GIT_COMMAND_FAILED", completed.stderr.strip())
    return completed.stdout.strip()


def capture_repo(repo: Path) -> RepoSnapshot:
    """Capture the current commit and porcelain status of ``repo``."""

    return RepoSnapshot(
        head=git(repo, "rev-parse", "HEAD"),
        status=tuple(git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()),
    )


def assert_pinned(snapshot: RepoSnapshot, repo: Path) -> None:
    """Reject an operation when the repository HEAD differs from its snapshot."""

    if snapshot.head != capture_repo(repo).head:
        _fail("HEAD_DRIFT", "repository HEAD changed after the snapshot was captured")


def changed_paths(repo: Path, base: str, candidate: str) -> set[str]:
    """Return the repository-relative paths changed between two revisions."""

    output = git(repo, "diff", "--name-only", "-z", "--no-renames", base, candidate, "--")
    return {path for path in output.split("\0") if path}


def working_tree_paths(repo: Path) -> set[str]:
    """Return every current path whose working-tree content differs from HEAD."""

    output = git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--no-renames",
        "-z",
    )
    paths: set[str] = set()
    for record in output.split("\0"):
        if not record:
            continue
        if len(record) < 4:
            _fail("GIT_STATUS_INVALID", "cannot parse repository status")
        paths.add(record[3:])
    return paths


def _resolve_commit(repo: Path, value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("COMMIT_REQUIRED", f"{field} must name a commit")
    try:
        return git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    except WorkflowError as exc:
        if exc.code == "GIT_COMMAND_FAILED":
            _fail("ACCEPTANCE_COMMIT_UNRESOLVED", f"cannot resolve {field}")
        raise


def _execution_repo(task: Mapping[str, object], role: str) -> Path:
    repository_root = Path(task["repository_root"]).resolve()
    source_worktree = task.get("source_worktree")
    if role == "terra":
        if task.get("task_type") != "REMEDIATION":
            _fail("TERRA_TASK_TYPE_INVALID", "Terra may run only for REMEDIATION tasks")
        if not isinstance(source_worktree, str) or not source_worktree.strip():
            _fail("SOURCE_WORKTREE_REQUIRED", "Terra requires an authorized source_worktree")
        return Path(source_worktree).resolve()
    if task.get("task_type") == "ACCEPTANCE" and isinstance(source_worktree, str):
        return Path(source_worktree).resolve()
    return repository_root


def assert_acceptance_candidate(task: Mapping[str, object], repo: Path) -> tuple[str, str] | None:
    """Resolve both acceptance revisions and require the candidate to be checked out."""

    if task.get("task_type") != "ACCEPTANCE":
        return None
    base = _resolve_commit(repo, task.get("base_commit"), "base_commit")
    candidate = _resolve_commit(repo, task.get("candidate_commit"), "candidate_commit")
    if git(repo, "rev-parse", "HEAD") != candidate:
        _fail(
            "ACCEPTANCE_CANDIDATE_HEAD_MISMATCH",
            "acceptance repository HEAD must equal the resolved candidate_commit",
        )
    return base, candidate


def _reject_dirty_input(repo: Path, code: str, detail: str) -> None:
    if working_tree_paths(repo):
        _fail(code, detail)


def assert_allowed_changes(changed: set[str], allowed: Sequence[str]) -> None:
    """Reject changed paths that do not fall under an approved file or prefix."""

    if not isinstance(changed, set) or any(not isinstance(path, str) for path in changed):
        _fail("INVALID_CHANGED_FILES", "changed paths must be a set of strings")
    if isinstance(allowed, str) or any(not isinstance(path, str) for path in allowed):
        _fail("INVALID_ALLOWED_PATHS", "allowed paths must be a sequence of strings")

    def is_allowed(path: str) -> bool:
        return any(
            path == scope or (scope.endswith("/") and path.startswith(scope))
            for scope in allowed
        )

    outside = sorted(path for path in changed if not is_allowed(path))
    if outside:
        _fail("OUT_OF_SCOPE_CHANGE", f"changed path is outside the allowed scope: {outside[0]}")


def _load_role_config(role: str) -> dict[str, object]:
    """Return the pinned configuration for one named workflow role."""

    try:
        import tomllib

        with ROLE_CONFIG_PATH.open("rb") as handle:
            config = tomllib.load(handle)
        role_config = config["roles"][role]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowError("INVALID_ROLE", f"unsupported or unreadable role {role}") from exc
    if not isinstance(role_config, dict):
        raise WorkflowError("INVALID_ROLE", f"unsupported role {role}")
    return role_config


def build_role_prompt(
    role: str,
    task: Mapping[str, object],
    contract: Mapping[str, object],
    evidence_paths: Sequence[Path],
) -> str:
    """Build the bounded prompt from only the supplied task, contract, and evidence."""

    role_config = _load_role_config(role)
    validate_task(task)
    if not isinstance(contract, Mapping):
        _fail("INVALID_CONTRACT", "contract must be an object")
    evidence = []
    for evidence_path in evidence_paths:
        path = Path(evidence_path)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorkflowError("EVIDENCE_READ_ERROR", f"cannot read evidence {path}") from exc
        evidence.append({"path": str(path), "sha256": digest})
    return "\n".join(
        (
            f"Role instructions: {role_config['instructions']}",
            f"Task envelope: {_canonical_json(dict(task))}",
            f"Task contract: {_canonical_json(dict(contract))}",
            f"Named evidence: {_canonical_json(evidence)}",
            f'Output "role" exactly as "{role}".',
            'Output "status" as exactly one of: '
            + ", ".join(role_config["allowed_statuses"])
            + ".",
            *(
                (
                    "For Luna L1, output 1 to 5 claims and exactly 1 counter_check unless status is BLOCKED; "
                    "if BLOCKED, output at most 5 claims and at most 1 counter_check. "
                    "Every claim must reference existing evidence and every counter_check must target an existing claim.",
                )
                if role == "luna" and task["verification_level"] == "L1"
                else ()
            ),
            "Read the named evidence files at the listed paths before evaluating the task.",
            "Use only the task contract and named evidence above; no additional source material is authorized.",
            "only output ai-result-1 JSON",
        )
    )


def build_codex_command(role: str, repo: Path, output_path: Path, schema_path: Path) -> list[str]:
    """Build the fixed, list-form command for a pinned Codex role."""

    role_config = _load_role_config(role)
    model = role_config.get("model")
    effort = role_config.get("reasoning_effort")
    sandbox = role_config.get("sandbox")
    if not all(isinstance(value, str) and value for value in (model, effort, sandbox)):
        _fail("INVALID_ROLE", f"role {role} has incomplete pinned configuration")
    return [
        "codex",
        "exec",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-s",
        sandbox,
        "-C",
        str(Path(repo)),
        "--json",
        "--output-schema",
        str(Path(schema_path)),
        "-o",
        str(Path(output_path)),
        "-",
    ]


def sanitized_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Pass only execution essentials, never business secrets, to Codex."""

    return {
        key: value
        for key, value in source.items()
        if key in SAFE_ENVIRONMENT_KEYS and isinstance(value, str)
    }


def _redact_log_text(value: str) -> str:
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    return _LONG_HIGH_ENTROPY.sub("[REDACTED]", redacted)


@dataclass(frozen=True)
class RunPaths:
    repo: Path
    output_path: Path
    schema_path: Path
    logs_dir: Path
    state_root: Path | None = None


def _validate_result_records(
    value: list[object],
    field: str,
    required: frozenset[str],
    *,
    type_values: frozenset[str] | None = None,
) -> None:
    for item in value:
        if not isinstance(item, Mapping) or set(item) != required:
            _fail("INVALID_ROLE_RESULT", f"{field} contains an incomplete record")
        for name in required:
            if name == "evidence_ids":
                identifiers = item[name]
                if (
                    not isinstance(identifiers, list)
                    or any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
                    or len(identifiers) != len(set(identifiers))
                ):
                    _fail("INVALID_ROLE_RESULT", "evidence_ids must be unique non-empty strings")
                continue
            item_value = item[name]
            if not isinstance(item_value, str) or not item_value.strip():
                _fail("INVALID_ROLE_RESULT", f"{field}.{name} must be a non-empty string")
        if type_values is not None and item["type"] not in type_values:
            _fail("INVALID_ROLE_RESULT", f"{field}.type is not supported")


def validate_role_result(
    role: str, result: Mapping[str, object], changed_files: set[str]
) -> None:
    """Reject malformed output, role/status confusion, and untrusted change claims."""

    role_config = _load_role_config(role)
    if not isinstance(result, Mapping):
        _fail("INVALID_ROLE_RESULT", "role result must be an object")
    fields = set(result)
    missing = sorted(RESULT_REQUIRED_FIELDS - fields)
    unknown = sorted(fields - RESULT_REQUIRED_FIELDS)
    if missing or unknown:
        field = missing[0] if missing else unknown[0]
        _fail("INVALID_ROLE_RESULT", f"unexpected result field {field}")
    if result["schema_version"] != "ai-result-1":
        _fail("INVALID_ROLE_RESULT", "schema_version must be ai-result-1")
    if result["role"] != role:
        _fail("ROLE_MISMATCH", f"result role does not match {role}")
    status = result["status"]
    allowed_statuses = role_config.get("allowed_statuses")
    if not isinstance(status, str) or not isinstance(allowed_statuses, list) or status not in allowed_statuses:
        _fail("ROLE_STATUS_MISMATCH", f"status is not allowed for {role}")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        _fail("INVALID_ROLE_RESULT", "summary must be a non-empty string")
    if not isinstance(result["recommended_next_state"], str) or not result["recommended_next_state"].strip():
        _fail("INVALID_ROLE_RESULT", "recommended_next_state must be a non-empty string")
    for field in (
        "claims",
        "evidence",
        "counter_checks",
        "changed_files",
        "blind_spots",
        "unresolved_questions",
    ):
        if not isinstance(result[field], list):
            _fail("INVALID_ROLE_RESULT", f"{field} must be an array")
    _validate_result_records(
        result["claims"],
        "claims",
        frozenset({"id", "kind", "text", "evidence_ids"}),
    )
    for claim in result["claims"]:
        if claim["kind"] not in {"FACT", "INFERENCE", "RECOMMENDATION"}:
            _fail("INVALID_ROLE_RESULT", "claims.kind is not supported")
    _validate_result_records(
        result["evidence"],
        "evidence",
        frozenset({"id", "type", "locator", "observation"}),
        type_values=frozenset({"FILE", "COMMAND", "HASH", "TEST"}),
    )
    _validate_result_records(
        result["counter_checks"],
        "counter_checks",
        frozenset({"target_claim_id", "method", "result"}),
    )
    declared_changes = result["changed_files"]
    if any(not isinstance(path, str) for path in declared_changes) or len(declared_changes) != len(set(declared_changes)):
        _fail("INVALID_ROLE_RESULT", "changed_files must be unique strings")
    for field in ("blind_spots", "unresolved_questions"):
        values = result[field]
        if any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
            _fail("INVALID_ROLE_RESULT", f"{field} must contain unique strings")
    if not isinstance(changed_files, set) or any(not isinstance(path, str) for path in changed_files):
        _fail("INVALID_CHANGED_FILES", "changed_files must be a set of strings")
    if role in READ_ONLY_ROLES and changed_files:
        _fail("READ_ONLY_ROLE_MODIFIED_REPO", f"read-only role {role} changed the repository")
    if set(declared_changes) != changed_files:
        _fail("CHANGED_FILES_MISMATCH", "declared changed_files differ from the real diff")


def validate_verification_package(
    role: str, task: Mapping[str, object], result: Mapping[str, object]
) -> None:
    """Enforce only the mechanically decidable parts of an evidence level."""

    validate_task(task)
    if role != "luna" or task["verification_level"] != "L1":
        return
    claims = result["claims"]
    evidence = result["evidence"]
    counter_checks = result["counter_checks"]
    blocked = result["status"] == "BLOCKED"
    if len(claims) > 5 or (not blocked and len(claims) < 1):
        _fail("INVALID_VERIFICATION_PACKAGE", "Luna L1 requires 1 to 5 claims unless blocked")
    if len(counter_checks) > 1 or (not blocked and len(counter_checks) != 1):
        _fail(
            "INVALID_VERIFICATION_PACKAGE",
            "Luna L1 requires exactly one counter-check unless blocked",
        )
    claim_ids = {claim["id"] for claim in claims}
    evidence_ids = {record["id"] for record in evidence}
    for claim in claims:
        referenced = claim["evidence_ids"]
        if not referenced or not set(referenced).issubset(evidence_ids):
            _fail(
                "INVALID_VERIFICATION_PACKAGE",
                "each Luna L1 claim must reference existing evidence",
            )
    if any(check["target_claim_id"] not in claim_ids for check in counter_checks):
        _fail(
            "INVALID_VERIFICATION_PACKAGE",
            "each Luna L1 counter-check must reference an existing claim",
        )


def _write_role_events(log_path: Path, stdout: object) -> None:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    text = stdout if isinstance(stdout, str) else ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(_redact_log_text(text))


def run_codex(role: str, task: dict, prompt: str, paths: RunPaths) -> dict:
    """Run one pinned Codex role and accept only a validated output document."""

    validate_task(task)
    if not isinstance(prompt, str):
        _fail("INVALID_PROMPT", "prompt must be a string")
    repo = Path(paths.repo).resolve()
    if repo != _execution_repo(task, role):
        _fail("ROLE_REPOSITORY_MISMATCH", "role repository does not match the task execution repository")
    if role == "terra":
        _assert_terra_worktree_authorized(task, repo, paths.state_root)
    if role in READ_ONLY_ROLES:
        _reject_dirty_input(repo, "DIRTY_READ_ONLY_REPOSITORY", "read-only role requires a clean repository")
    if task["task_type"] == "ACCEPTANCE":
        _reject_dirty_input(repo, "DIRTY_ACCEPTANCE_REPOSITORY", "acceptance requires a clean repository")
        assert_acceptance_candidate(task, repo)
    if role == "terra":
        _reject_dirty_input(repo, "DIRTY_TERRA_WORKTREE", "Terra requires a clean source_worktree")
    before_run = capture_repo(repo)
    before_changes = working_tree_paths(repo)
    attempt_id = f"{role}-{time.time_ns()}-{uuid.uuid4().hex}"
    attempt_output = Path(paths.output_path).parent / "attempts" / f"{attempt_id}.json"
    attempt_events = Path(paths.logs_dir) / f"{attempt_id}.jsonl"
    attempt_started_ns = time.time_ns()
    attempt_output.parent.mkdir(parents=True, exist_ok=True)
    if attempt_output.exists():
        _fail("ATTEMPT_OUTPUT_COLLISION", "role attempt output path already exists")
    result: dict | None = None
    try:
        command = build_codex_command(role, repo, attempt_output, paths.schema_path)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                input=prompt,
                text=True,
                timeout=CODEX_TIMEOUT_SECONDS,
                env=sanitized_environment(os.environ),
                cwd=str(repo),
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            _write_role_events(attempt_events, exc.stdout)
            raise WorkflowError("CODEX_TIMEOUT", f"{role} exceeded {CODEX_TIMEOUT_SECONDS} seconds") from exc
        _write_role_events(attempt_events, completed.stdout)
        if completed.returncode != 0:
            raise WorkflowError("CODEX_EXIT_NONZERO", f"{role} exited with code {completed.returncode}")
        try:
            output_stat = attempt_output.stat()
        except OSError as exc:
            raise WorkflowError("MISSING_FRESH_ROLE_OUTPUT", f"{role} did not produce a fresh JSON result") from exc
        if output_stat.st_mtime_ns < attempt_started_ns:
            _fail("MISSING_FRESH_ROLE_OUTPUT", "role attempt output predates the attempt")
        try:
            result = json.loads(attempt_output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError("INVALID_ROLE_RESULT", f"{role} did not produce valid JSON") from exc
        if not isinstance(result, dict):
            _fail("INVALID_ROLE_RESULT", "role output must be an object")
    finally:
        after_run = capture_repo(repo)
        after_changes = working_tree_paths(repo)
        if before_run.head != after_run.head:
            _fail("HEAD_DRIFT", "repository HEAD changed during the role run")
        if role in READ_ONLY_ROLES and before_run != after_run:
            _fail("READ_ONLY_ROLE_MODIFIED_REPO", f"read-only role {role} changed the repository")
        if task["task_type"] == "ACCEPTANCE":
            assert_acceptance_candidate(task, repo)
        if role == "terra":
            actual_changes = after_changes - before_changes
            assert_allowed_changes(actual_changes, task["allowed_write_paths"])
        else:
            actual_changes = after_changes - before_changes
    if result is None:
        _fail("INVALID_ROLE_RESULT", "role did not return a result")
    validate_role_result(role, result, actual_changes)
    validate_verification_package(role, task, result)
    atomic_write_json(paths.output_path, result)
    return result


def _require_nonempty_string(task: Mapping[str, object], field: str) -> None:
    value = task[field]
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")


def _require_nullable_string(task: Mapping[str, object], field: str) -> None:
    value = task[field]
    if value is not None and not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string or null")
    if isinstance(value, str) and not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty when provided")


def _require_unique_string_array(
    task: Mapping[str, object],
    field: str,
    *,
    allowed: frozenset[str] | None = None,
    nonempty: bool = False,
) -> None:
    value = task[field]
    if not isinstance(value, list):
        _fail("INVALID_TYPE", f"{field} must be an array")
    if nonempty and not value:
        _fail("EMPTY_ARRAY", f"{field} must not be empty")
    if any(not isinstance(item, str) for item in value):
        _fail("INVALID_TYPE", f"{field} items must be strings")
    if len(value) != len(set(value)):
        _fail("DUPLICATE_ITEM", f"{field} must not contain duplicates")
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            _fail("INVALID_ENUM", f"{field} contains unsupported value {unknown[0]}")


def validate_task(task: Mapping[str, object]) -> None:
    """Validate a task envelope against the frozen ``ai-task-1`` contract."""

    if not isinstance(task, Mapping):
        _fail("INVALID_TASK", "task must be an object")

    fields = set(task)
    unknown = sorted(fields - TASK_FIELDS)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(TASK_FIELDS - fields)
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")

    if task["schema_version"] != "ai-task-1":
        _fail("SCHEMA_VERSION", "schema_version must be ai-task-1")
    for field in ("task_id", "objective", "repository_root"):
        _require_nonempty_string(task, field)
    if not TASK_ID_PATTERN.fullmatch(task["task_id"]):
        _fail("INVALID_TASK_ID", "task_id must match AWF-YYYYMMDD-NNN")

    task_type = task["task_type"]
    if not isinstance(task_type, str) or task_type not in TASK_TYPES:
        _fail("INVALID_ENUM", "task_type is not supported")
    level = task["verification_level"]
    if not isinstance(level, str) or level not in VERIFICATION_LEVELS:
        _fail("INVALID_ENUM", "verification_level is not supported")

    for field in ("source_worktree", "base_commit", "candidate_commit"):
        _require_nullable_string(task, field)
    _require_unique_string_array(task, "authoritative_files", nonempty=True)
    _require_unique_string_array(task, "allowed_write_paths")
    _require_unique_string_array(task, "forbidden_actions")
    _require_unique_string_array(task, "risk_flags", allowed=RISK_FLAGS)
    _require_unique_string_array(task, "acceptance_commands")
    _require_unique_string_array(task, "human_gates", allowed=HUMAN_GATES)

    if task_type == "ACCEPTANCE":
        if not task["base_commit"] or not task["candidate_commit"]:
            _fail("COMMIT_REQUIRED", "ACCEPTANCE requires base_commit and candidate_commit")
    if task_type == "REMEDIATION" and not task["allowed_write_paths"]:
        _fail("WRITE_PATH_REQUIRED", "REMEDIATION requires allowed_write_paths")


def load_task(path: Path) -> dict[str, object]:
    """Read and validate one JSON task envelope."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkflowError("TASK_READ_ERROR", f"cannot read task: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError("INVALID_JSON", f"invalid task JSON at line {exc.lineno}") from exc
    if not isinstance(payload, dict):
        _fail("INVALID_TASK", "task JSON must contain an object")
    validate_task(payload)
    return payload


OWNER_ONLY_STATES = frozenset(
    {
        "APPROVED_FOR_EXECUTION",
        "REWORK_AUTHORIZED",
        "ESCALATION_AUTHORIZED",
        "DEFERRED",
        "CLOSED",
    }
)
OWNER_GATED_TRANSITIONS = frozenset(
    {
        ("AWAITING_OWNER_DECISION", "APPROVED_FOR_EXECUTION"),
        ("AWAITING_OWNER_DECISION", "REWORK_AUTHORIZED"),
        ("AWAITING_OWNER_DECISION", "ESCALATION_AUTHORIZED"),
        ("AWAITING_OWNER_DECISION", "DEFERRED"),
        ("AWAITING_OWNER_DECISION", "CLOSED"),
        ("AWAITING_OWNER_DECISION", "ABORTED"),
        ("DEFERRED", "TASK_VALIDATED"),
        ("DEFERRED", "CLOSED"),
        ("DEFERRED", "ABORTED"),
        ("APPROVED_FOR_EXECUTION", "WORKTREE_READY"),
        ("REWORK_AUTHORIZED", "IMPLEMENTATION_RUNNING"),
        ("ESCALATION_AUTHORIZED", "PLAN_OR_REVIEW_RUNNING"),
    }
)
TRANSITIONS = {
    "DRAFT": frozenset({"TASK_VALIDATED", "ABORTED"}),
    "TASK_VALIDATED": frozenset(
        {"EVIDENCE_RUNNING", "PLAN_OR_REVIEW_RUNNING", "AWAITING_OWNER_DECISION", "BLOCKED", "ABORTED"}
    ),
    "EVIDENCE_RUNNING": frozenset({"EVIDENCE_READY", "BLOCKED", "ABORTED"}),
    "EVIDENCE_READY": frozenset({"PLAN_OR_REVIEW_RUNNING", "BLOCKED", "ABORTED"}),
    "PLAN_OR_REVIEW_RUNNING": frozenset(
        {"PLAN_READY", "REVIEW_READY", "BLOCKED", "ESCALATION_PROPOSED"}
    ),
    "PLAN_READY": frozenset({"AWAITING_OWNER_DECISION"}),
    "REVIEW_READY": frozenset({"AWAITING_OWNER_DECISION"}),
    "ESCALATION_PROPOSED": frozenset({"AWAITING_OWNER_DECISION"}),
    "AWAITING_OWNER_DECISION": frozenset(
        {
            "APPROVED_FOR_EXECUTION",
            "REWORK_AUTHORIZED",
            "ESCALATION_AUTHORIZED",
            "DEFERRED",
            "CLOSED",
            "ABORTED",
        }
    ),
    "APPROVED_FOR_EXECUTION": frozenset({"WORKTREE_READY", "ABORTED"}),
    "REWORK_AUTHORIZED": frozenset({"IMPLEMENTATION_RUNNING", "ABORTED"}),
    "ESCALATION_AUTHORIZED": frozenset({"PLAN_OR_REVIEW_RUNNING", "ABORTED"}),
    "DEFERRED": frozenset({"TASK_VALIDATED", "CLOSED", "ABORTED"}),
    "WORKTREE_READY": frozenset({"IMPLEMENTATION_RUNNING", "BLOCKED", "ABORTED"}),
    "IMPLEMENTATION_RUNNING": frozenset({"IMPLEMENTED_CANDIDATE", "BLOCKED", "NEEDS_REPLAN"}),
    "IMPLEMENTED_CANDIDATE": frozenset({"PRECHECK_RUNNING", "BLOCKED"}),
    "PRECHECK_RUNNING": frozenset({"PRECHECK_READY", "BLOCKED"}),
    "PRECHECK_READY": frozenset({"PLAN_OR_REVIEW_RUNNING", "BLOCKED"}),
    "NEEDS_REPLAN": frozenset({"AWAITING_OWNER_DECISION", "BLOCKED", "ABORTED"}),
}
WORKFLOW_STATES = frozenset(TRANSITIONS) | frozenset(
    state for targets in TRANSITIONS.values() for state in targets
)


def next_state(current: str, target: str, *, owner_authorized: bool) -> str:
    """Return ``target`` when the explicit transition is authorized."""

    if not isinstance(current, str) or not isinstance(target, str):
        _fail("INVALID_STATE", "states must be strings")
    allowed = TRANSITIONS.get(current)
    if allowed is None:
        _fail("INVALID_STATE", f"unknown current state {current}")
    if target not in allowed:
        _fail("INVALID_TRANSITION", f"cannot transition from {current} to {target}")
    if (target in OWNER_ONLY_STATES or (current, target) in OWNER_GATED_TRANSITIONS) and not owner_authorized:
        _fail("OWNER_AUTHORIZATION_REQUIRED", f"owner authorization required for {target}")
    return target


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("INVALID_RECORD", f"record is not JSON serializable: {exc}") from exc


def atomic_write_json(path: Path, value: object) -> None:
    """Write JSON through a same-directory fsynced temporary file and replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise WorkflowError("ATOMIC_WRITE_FAILED", f"cannot write {target.name}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """Append one compact, fsynced JSON object without rewrite/delete support."""

    if not isinstance(record, Mapping):
        raise WorkflowError("INVALID_RECORD", "JSONL record must be an object")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical_json(dict(record)) + "\n"
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkflowError("APPEND_FAILED", f"cannot append {target.name}") from exc


class WorkflowStore:
    """Filesystem-backed task store with append-only event and decision ledgers."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise WorkflowError("INVALID_TASK_ID", "task_id must match AWF-YYYYMMDD-NNN")

    def _task_dir(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self.root / task_id

    def _require_task(self, task_id: str) -> Path:
        task_dir = self._task_dir(task_id)
        if not task_dir.is_dir():
            raise WorkflowError("TASK_NOT_FOUND", f"task {task_id} does not exist")
        return task_dir

    def create_task(self, task: dict) -> Path:
        if not isinstance(task, dict):
            raise WorkflowError("INVALID_TASK", "task must be an object")
        validate_task(task)
        task_id = task["task_id"]
        task_dir = self._task_dir(task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            task_dir.mkdir()
        except FileExistsError as exc:
            raise WorkflowError("TASK_EXISTS", f"task {task_id} already exists") from exc
        try:
            task_path = task_dir / "task.json"
            atomic_write_json(task_path, task)
            return task_path
        except Exception:
            # Leave no partial task directory when initial canonical write fails.
            try:
                task_dir.rmdir()
            except OSError:
                pass
            raise

    def append_event(self, task_id: str, event: dict) -> None:
        task_dir = self._require_task(task_id)
        append_jsonl(task_dir / "events.jsonl", event)

    def record_decision(self, task_id: str, decision: dict) -> None:
        task_dir = self._require_task(task_id)
        append_jsonl(task_dir / "human-decisions.jsonl", decision)

    def metrics_path(self, task_id: str) -> Path:
        return self._require_task(task_id) / "metrics.json"

    @contextlib.contextmanager
    def lock(self, task_id: str):
        task_dir = self._require_task(task_id)
        lock_path = task_dir / ".lock"
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise WorkflowError("LOCK_FAILED", f"cannot open lock for {task_id}") from exc
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowError("TASK_ALREADY_RUNNING", f"task {task_id} is already running") from exc
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise WorkflowError("TASK_ALREADY_RUNNING", f"task {task_id} is already running") from exc
                raise WorkflowError("LOCK_FAILED", f"cannot lock task {task_id}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _parse_metric_number(value: object) -> int | float | None:
    """Accept only explicit, finite JSON numbers; never derive usage estimates."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _metric_duration(value: object) -> float:
    parsed = _parse_metric_number(value)
    if parsed is None or parsed < 0:
        return 0.0
    return float(parsed)


def _metric_identifiers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item.strip()))


def _metric_claim_identifiers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    identifiers = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"].strip():
            identifiers.append(item["id"])
    return list(dict.fromkeys(identifiers))


def _normalize_metric_run(run: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(run, Mapping):
        _fail("INVALID_METRICS_RUN", "run must be an object")
    role = run.get("role")
    if not isinstance(role, str) or not role.strip():
        _fail("INVALID_METRICS_RUN", "run.role must be a non-empty string")
    token_usage = _parse_metric_number(run.get("token_usage"))
    if token_usage is not None and token_usage < 0:
        token_usage = None
    finding_ids = _metric_identifiers(run.get("finding_ids"))
    if not finding_ids:
        finding_ids = _metric_identifiers(run.get("findings"))
    if not finding_ids and role == "luna":
        finding_ids = _metric_claim_identifiers(run.get("claims"))
    adopted_ids = _metric_identifiers(run.get("adopted_luna_finding_ids"))
    if not adopted_ids:
        adopted_ids = _metric_identifiers(run.get("adopted_finding_ids"))
    period = run.get("period")
    if period not in {"calibration", "experiment"}:
        period = "experiment"
    workflow_state = run.get("workflow_state")
    activity = run.get("activity")
    status = run.get("status")
    return {
        "role": role,
        "timestamp_utc": _utc_timestamp(),
        "duration_seconds": _metric_duration(run.get("duration_seconds")),
        "token_usage": token_usage,
        "period": period,
        "finding_ids": finding_ids,
        "adopted_luna_finding_ids": adopted_ids,
        "luna_self_check": role == "luna"
        and (activity == "self_check" or workflow_state == "PRECHECK_RUNNING"),
        "sol_verification": role in {"sol_reviewer", "sol_xhigh"},
        "semantic_rework": run.get("semantic_rework") is True,
        "full_suite_run": run.get("full_suite_run") is True,
        "status": status if isinstance(status, str) else None,
    }


def _load_metrics_document(path: Path, task_id: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "schema_version": METRICS_SCHEMA_VERSION,
            "task_id": task_id,
            "token_usage": None,
            "runs": [],
        }
    except OSError as exc:
        raise WorkflowError("METRICS_READ_ERROR", f"cannot read metrics for {task_id}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError("INVALID_METRICS_RECORD", f"invalid metrics JSON for {task_id}") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != METRICS_SCHEMA_VERSION
        or document.get("task_id") != task_id
        or not isinstance(document.get("runs"), list)
    ):
        _fail("INVALID_METRICS_RECORD", f"invalid metrics document for {task_id}")
    return document


def record_metrics(task_id: str, run: Mapping[str, object]) -> None:
    """Record one measured role attempt in the task's existing workflow store."""

    store = WorkflowStore(WORKFLOW_STATE_ROOT)
    path = store.metrics_path(task_id)
    document = _load_metrics_document(path, task_id)
    normalized_run = _normalize_metric_run(run)
    document["runs"].append(normalized_run)
    # The top-level value describes this newest raw attempt. It is null when
    # Codex JSONL did not explicitly provide a parseable usage number.
    document["token_usage"] = normalized_run["token_usage"]
    atomic_write_json(path, document)


def _read_metrics_runs(task_dir: Path, task_id: str) -> list[dict[str, object]]:
    document = _load_metrics_document(task_dir / "metrics.json", task_id)
    runs = document["runs"]
    records: list[dict[str, object]] = []
    for run in runs:
        if isinstance(run, dict):
            records.append(run)
    return records


def _read_task_events(task_dir: Path, task_id: str) -> list[dict[str, object]]:
    path = task_dir / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise WorkflowError("EVENT_READ_ERROR", f"cannot read events for {task_id}") from exc
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError("INVALID_EVENT_RECORD", f"invalid event JSON for {task_id}") from exc
        if not isinstance(record, dict):
            _fail("INVALID_EVENT_RECORD", "event record must be an object")
        records.append(record)
    return records


def _is_stop_line_event(record: Mapping[str, object]) -> bool:
    event_type = record.get("event_type")
    if isinstance(event_type, str) and any(
        marker in event_type.upper() for marker in ("STOP", "EXHAUSTED", "ABORT")
    ):
        return True
    return record.get("new_state") == "BLOCKED"


def aggregate_metrics(root: Path) -> dict:
    """Aggregate task-local metrics and events without creating another store."""

    root = Path(root)
    role_calls: dict[str, int] = {}
    periods: dict[str, set[str]] = {}
    terra_first_status: dict[str, str | None] = {}
    luna_finding_ids: set[tuple[str, str]] = set()
    adopted_luna_finding_ids: set[tuple[str, str]] = set()
    luna_self_check_seconds = 0.0
    sol_verification_seconds = 0.0
    semantic_reworks = 0
    full_suite_runs = 0
    end_to_end_seconds = 0.0
    stop_line_events: list[dict[str, object]] = []
    try:
        task_dirs = sorted(
            (path for path in root.iterdir() if path.is_dir() and TASK_ID_PATTERN.fullmatch(path.name)),
            key=lambda path: path.name,
        )
    except FileNotFoundError:
        task_dirs = []
    except OSError as exc:
        raise WorkflowError("METRICS_READ_ERROR", f"cannot read metrics root {root}") from exc
    for task_dir in task_dirs:
        task_id = task_dir.name
        for run in _read_metrics_runs(task_dir, task_id):
            role = run.get("role")
            if not isinstance(role, str):
                continue
            role_calls[role] = role_calls.get(role, 0) + 1
            period = run.get("period")
            if period in {"calibration", "experiment"}:
                periods.setdefault(task_id, set()).add(period)
            duration = _metric_duration(run.get("duration_seconds"))
            end_to_end_seconds += duration
            if run.get("luna_self_check") is True:
                luna_self_check_seconds += duration
            if run.get("sol_verification") is True:
                sol_verification_seconds += duration
            if run.get("semantic_rework") is True:
                semantic_reworks += 1
            if run.get("full_suite_run") is True:
                full_suite_runs += 1
            if role == "luna":
                luna_finding_ids.update(
                    (task_id, finding_id) for finding_id in _metric_identifiers(run.get("finding_ids"))
                )
            if role.startswith("sol_"):
                adopted_luna_finding_ids.update(
                    (task_id, finding_id)
                    for finding_id in _metric_identifiers(run.get("adopted_luna_finding_ids"))
                )
            if role == "terra" and task_id not in terra_first_status:
                status = run.get("status")
                terra_first_status[task_id] = status if isinstance(status, str) else None
        for event in _read_task_events(task_dir, task_id):
            if event.get("new_state") == "NEEDS_REPLAN" or event.get("event_type") == "SEMANTIC_REWORK":
                semantic_reworks += 1
            if event.get("event_type") == "FULL_SUITE_COMPLETED":
                full_suite_runs += 1
            if _is_stop_line_event(event):
                stop_line_events.append({"task_id": task_id, "event": event})
    calibration_tasks = sum("calibration" in task_periods for task_periods in periods.values())
    experiment_tasks = sum("experiment" in task_periods for task_periods in periods.values())
    first_delivery_passes = sum(
        status == "IMPLEMENTED_CANDIDATE" for status in terra_first_status.values()
    )
    first_delivery_total = len(terra_first_status)
    return {
        "calibration_task_count": calibration_tasks,
        "experiment_task_count": experiment_tasks,
        "role_calls": dict(sorted(role_calls.items())),
        "sol_participation_count": sum(
            count for role, count in role_calls.items() if role.startswith("sol_")
        ),
        "first_delivery_pass_rate": (
            first_delivery_passes / first_delivery_total if first_delivery_total else None
        ),
        "luna_unique_findings": len(luna_finding_ids),
        "luna_findings_adopted_by_sol": len(luna_finding_ids & adopted_luna_finding_ids),
        "luna_self_check_seconds": luna_self_check_seconds,
        "sol_verification_seconds": sol_verification_seconds,
        "semantic_reworks": semantic_reworks,
        "full_suite_runs": full_suite_runs,
        "end_to_end_seconds": end_to_end_seconds,
        "stop_line_events": stop_line_events,
    }


def render_report(metrics: Mapping[str, object]) -> str:
    """Render the one human-facing experiment report from aggregate metrics."""

    if not isinstance(metrics, Mapping):
        _fail("INVALID_METRICS", "metrics must be an object")
    role_calls = metrics.get("role_calls")
    if not isinstance(role_calls, Mapping):
        _fail("INVALID_METRICS", "metrics.role_calls must be an object")
    lines = [
        "# AI Workflow Experiment Report",
        "",
        "> This calibration report proves only that the Luna read-only path can run; it does not demonstrate cost reduction or efficiency gains.",
        "",
        "## Cohorts",
        "",
        f"- Calibration tasks: {metrics.get('calibration_task_count', 0)}",
        f"- Experiment tasks: {metrics.get('experiment_task_count', 0)}",
        "",
        "## Role calls",
        "",
    ]
    lines.extend(f"- {role}: {count}" for role, count in sorted(role_calls.items()))
    pass_rate = metrics.get("first_delivery_pass_rate")
    pass_rate_text = "n/a" if pass_rate is None else f"{float(pass_rate):.1%}"
    lines.extend(
        (
            f"- Sol participation: {metrics.get('sol_participation_count', 0)}",
            "",
            "## Outcomes",
            "",
            f"- First delivery pass rate: {pass_rate_text}",
            f"- Repeated full-suite runs: {metrics.get('full_suite_runs', 0)}",
            f"- Semantic reworks: {metrics.get('semantic_reworks', 0)}",
            f"- End-to-end seconds: {float(metrics.get('end_to_end_seconds', 0.0)):.3f}",
            "",
            "## Luna value and review cost",
            "",
            f"- Luna unique findings: {metrics.get('luna_unique_findings', 0)}",
            f"- Luna findings adopted by Sol: {metrics.get('luna_findings_adopted_by_sol', 0)}",
            f"- Luna self-check seconds: {float(metrics.get('luna_self_check_seconds', 0.0)):.3f}",
            f"- Sol verification seconds: {float(metrics.get('sol_verification_seconds', 0.0)):.3f}",
            "",
            "## Stop-line events",
            "",
        )
    )
    stop_line_events = metrics.get("stop_line_events", [])
    if isinstance(stop_line_events, list) and stop_line_events:
        lines.extend(f"- {_canonical_json(event)}" for event in stop_line_events)
    else:
        lines.append("- None")
    return _redact_log_text("\n".join(lines) + "\n")


FAKE_ROLE_RESULTS = {
    "luna": ("SUPPORTED", "EVIDENCE_READY"),
    "terra": ("IMPLEMENTED_CANDIDATE", "PRECHECK_RUNNING"),
    "sol_planner": ("PLAN_READY", "AWAITING_OWNER_DECISION"),
    "sol_reviewer": ("ACCEPTANCE_RECOMMENDED", "AWAITING_OWNER_DECISION"),
    "sol_xhigh": ("OPTION_A", "ESCALATION_PROPOSED"),
}


class FakeRunner:
    """Deterministic local runner used by tests; it never calls a model."""

    def run(self, role: str, task: dict) -> dict[str, object]:
        if role not in FAKE_ROLE_RESULTS:
            raise WorkflowError("INVALID_ROLE", f"unsupported role {role}")
        validate_task(task)
        status, next_state_value = FAKE_ROLE_RESULTS[role]
        result = {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": f"Fake {role} result for {task['task_id']}",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": next_state_value,
        }
        if role == "luna" and task["verification_level"] == "L1":
            result["claims"] = [
                {
                    "id": "claim-1",
                    "kind": "FACT",
                    "text": f"Fake bounded evidence for {task['task_id']}",
                    "evidence_ids": ["evidence-1"],
                }
            ]
            result["evidence"] = [
                {
                    "id": "evidence-1",
                    "type": "FILE",
                    "locator": task["authoritative_files"][0],
                    "observation": "Fake runner checked the authorized evidence fixture.",
                }
            ]
            result["counter_checks"] = [
                {
                    "target_claim_id": "claim-1",
                    "method": "Check the fixture for a contradiction.",
                    "result": "No contradiction found in the fake fixture.",
                }
            ]
        return result


class Runner(Protocol):
    """The small, injectable boundary used by the gated orchestrator."""

    is_live_model: bool

    def run(self, role: str, task: dict[str, object]) -> Mapping[str, object]:
        """Return one ai-result-1-compatible role result."""


@dataclass
class RetryBudget:
    """The one-time retry allowances specified by the workflow contract."""

    technical_retries: int = 0
    implementation_reworks: int = 0
    cross_model_escalations: int = 0

    def _consume(self, field: str, detail: str) -> None:
        if getattr(self, field) >= 1:
            raise WorkflowError("RETRY_BUDGET_EXHAUSTED", detail)
        setattr(self, field, getattr(self, field) + 1)

    def consume_technical(self) -> None:
        self._consume("technical_retries", "technical")

    def consume_rework(self) -> None:
        self._consume("implementation_reworks", "implementation")

    def consume_escalation(self) -> None:
        self._consume("cross_model_escalations", "escalation")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_event_records(store: WorkflowStore, task_id: str) -> list[dict[str, object]]:
    path = store._require_task(task_id) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise WorkflowError("EVENT_READ_ERROR", f"cannot read events for {task_id}") from exc
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError("INVALID_EVENT_RECORD", f"invalid event JSON for {task_id}") from exc
        if not isinstance(record, dict):
            _fail("INVALID_EVENT_RECORD", "event record must be an object")
        records.append(record)
    return records


def _current_state(store: WorkflowStore, task_id: str) -> str:
    state = "DRAFT"
    for record in _load_event_records(store, task_id):
        next_value = record.get("new_state")
        if next_value is not None:
            if not isinstance(next_value, str) or next_value not in WORKFLOW_STATES:
                _fail("INVALID_EVENT_RECORD", "event new_state is invalid")
            state = next_value
    return state


def _budget_from_events(store: WorkflowStore, task_id: str) -> RetryBudget:
    for record in reversed(_load_event_records(store, task_id)):
        raw_budget = record.get("retry_budget")
        if raw_budget is None:
            continue
        if not isinstance(raw_budget, Mapping):
            _fail("INVALID_EVENT_RECORD", "retry_budget must be an object")
        values = []
        for field in ("technical_retries", "implementation_reworks", "cross_model_escalations"):
            value = raw_budget.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _fail("INVALID_EVENT_RECORD", f"retry_budget.{field} must be a non-negative integer")
            values.append(value)
        return RetryBudget(*values)
    return RetryBudget()


def _budget_record(budget: RetryBudget) -> dict[str, int]:
    return {
        "technical_retries": budget.technical_retries,
        "implementation_reworks": budget.implementation_reworks,
        "cross_model_escalations": budget.cross_model_escalations,
    }


def _task_sha256(store: WorkflowStore, task_id: str) -> str:
    task_path = store._require_task(task_id) / "task.json"
    try:
        return hashlib.sha256(task_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkflowError("TASK_READ_ERROR", f"cannot hash task {task_id}") from exc


def _append_state_event(
    store: WorkflowStore,
    task_id: str,
    *,
    event_type: str,
    previous_state: str,
    new_state: str,
    budget: RetryBudget,
    role: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
) -> str:
    event: dict[str, object] = {
        "event_type": event_type,
        "timestamp_utc": _utc_timestamp(),
        "previous_state": previous_state,
        "new_state": new_state,
        "task_sha256": _task_sha256(store, task_id),
        "retry_budget": _budget_record(budget),
    }
    if role is not None:
        event["role"] = role
    if status is not None:
        event["status"] = status
    if error_code is not None:
        event["error_code"] = error_code
    store.append_event(task_id, event)
    return new_state


def _transition(
    store: WorkflowStore,
    task_id: str,
    current: str,
    target: str,
    budget: RetryBudget,
    *,
    event_type: str = "STATE_TRANSITION",
    owner_authorized: bool = False,
) -> str:
    target = next_state(current, target, owner_authorized=owner_authorized)
    return _append_state_event(
        store,
        task_id,
        event_type=event_type,
        previous_state=current,
        new_state=target,
        budget=budget,
    )


def _load_latest_decision(store: WorkflowStore, task_id: str) -> dict[str, object] | None:
    path = store._require_task(task_id) / "human-decisions.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError("DECISION_READ_ERROR", f"cannot read decisions for {task_id}") from exc
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise WorkflowError("INVALID_DECISION_RECORD", f"invalid decision JSON for {task_id}") from exc
    if not isinstance(record, dict):
        _fail("INVALID_DECISION_RECORD", "decision record must be an object")
    return record


def _authorization_is_recorded(
    store: WorkflowStore, task_id: str, state: str, decision: str
) -> bool:
    record = _load_latest_decision(store, task_id)
    return bool(
        record
        and record.get("decision") == decision
        and record.get("new_state") == state
        and isinstance(record.get("actor"), str)
        and record["actor"].strip()
        and record.get("task_sha256") == _task_sha256(store, task_id)
    )


def _expected_worktree(task: Mapping[str, object]) -> Path:
    return Path(task["repository_root"]).resolve() / ".codex-worktrees" / str(task["task_id"]).lower()


def _assert_terra_worktree_authorized(
    task: Mapping[str, object], repo: Path, state_root: Path | None
) -> None:
    """Require Terra to use the one worktree bound to a verified owner decision."""

    expected_worktree = _expected_worktree(task)
    if Path(repo).resolve() != expected_worktree:
        _fail("UNAUTHORIZED_SOURCE_WORKTREE", "Terra source_worktree is not task-scoped")
    if state_root is None:
        _fail("WORKFLOW_STORE_REQUIRED", "Terra requires the verified workflow store")
    store = WorkflowStore(state_root)
    task_id = str(task["task_id"])
    stored_task = load_task(store._require_task(task_id) / "task.json")
    if stored_task != dict(task):
        _fail("TASK_STORE_MISMATCH", "Terra task does not match the stored task envelope")
    if not _authorization_is_recorded(
        store, task_id, "APPROVED_FOR_EXECUTION", "approve_execution"
    ):
        _fail("APPROVED_FOR_EXECUTION_REQUIRED", "Terra has no verified execution authorization")
    if _current_state(store, task_id) not in {
        "APPROVED_FOR_EXECUTION",
        "WORKTREE_READY",
        "IMPLEMENTATION_RUNNING",
    }:
        _fail("TERRA_STATE_NOT_AUTHORIZED", "Terra is not in an implementation state")


def create_worktree(
    task: dict, owner_authorized: bool, *, store: WorkflowStore | None = None
) -> Path:
    """Create the approved worktree at the frozen task base, never at current HEAD."""

    if not owner_authorized:
        _fail("OWNER_AUTHORIZATION_REQUIRED", "owner authorization is required before creating a worktree")
    validate_task(task)
    if task["task_type"] != "REMEDIATION":
        _fail("WORKTREE_TASK_TYPE_INVALID", "only remediation tasks may create a worktree")
    task_id = task["task_id"]
    verified_store = store or WorkflowStore(WORKFLOW_STATE_ROOT)
    stored_task = load_task(verified_store._require_task(task_id) / "task.json")
    if stored_task != task:
        _fail("TASK_STORE_MISMATCH", "worktree task does not match the stored task envelope")
    if _current_state(verified_store, task_id) != "APPROVED_FOR_EXECUTION":
        _fail("APPROVED_FOR_EXECUTION_REQUIRED", "worktree creation requires APPROVED_FOR_EXECUTION")
    if not _authorization_is_recorded(
        verified_store, task_id, "APPROVED_FOR_EXECUTION", "approve_execution"
    ):
        _fail("APPROVED_FOR_EXECUTION_REQUIRED", "worktree requires a verified execution decision")
    repository_root = Path(task["repository_root"]).resolve()
    base_commit = _resolve_commit(repository_root, task["base_commit"], "base_commit")
    worktree = _expected_worktree(task)
    source_worktree = task["source_worktree"]
    if not isinstance(source_worktree, str) or Path(source_worktree).resolve() != worktree:
        _fail("SOURCE_WORKTREE_MISMATCH", "task source_worktree must be the task-scoped worktree")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(
        repository_root,
        "worktree",
        "add",
        "-b",
        f"aiwf/{str(task_id).lower()}",
        str(worktree),
        base_commit,
    )
    return worktree


def _role_for_plan_or_review(
    store: WorkflowStore, task_id: str, task: Mapping[str, object]
) -> str:
    task_type = task["task_type"]
    if _authorization_is_recorded(
        store, task_id, "ESCALATION_AUTHORIZED", "authorize_escalation"
    ):
        return "sol_xhigh"
    if task_type == "PLAN":
        return "sol_planner"
    if task_type == "ACCEPTANCE":
        return "sol_reviewer"
    roles = {record.get("role") for record in _load_event_records(store, task_id)}
    if task.get("risk_flags") and "sol_planner" not in roles:
        return "sol_planner"
    return "sol_reviewer"


def _run_role_with_technical_retry(
    store: WorkflowStore,
    task_id: str,
    task: dict[str, object],
    state: str,
    role: str,
    runner: Runner,
    budget: RetryBudget,
) -> tuple[Mapping[str, object] | None, str]:
    """Run one role, allowing only the single persisted technical retry."""

    while True:
        try:
            guarded_repo: Path | None = None
            before_snapshot: RepoSnapshot | None = None
            before_changes: set[str] | None = None
            if getattr(runner, "is_live_model", False):
                guarded_repo = _execution_repo(task, role)
                if role == "terra":
                    _assert_terra_worktree_authorized(task, guarded_repo, WORKFLOW_STATE_ROOT)
                    _reject_dirty_input(
                        guarded_repo,
                        "DIRTY_TERRA_WORKTREE",
                        "Terra requires a clean source_worktree",
                    )
                if role in READ_ONLY_ROLES:
                    _reject_dirty_input(
                        guarded_repo,
                        "DIRTY_READ_ONLY_REPOSITORY",
                        "read-only role requires a clean repository",
                    )
                if task["task_type"] == "ACCEPTANCE":
                    _reject_dirty_input(
                        guarded_repo,
                        "DIRTY_ACCEPTANCE_REPOSITORY",
                        "acceptance requires a clean repository",
                    )
                    assert_acceptance_candidate(task, guarded_repo)
                before_snapshot = capture_repo(guarded_repo)
                before_changes = working_tree_paths(guarded_repo)
            started_monotonic = time.monotonic()
            try:
                result = runner.run(role, task)
            finally:
                if guarded_repo is not None and before_snapshot is not None and before_changes is not None:
                    after_snapshot = capture_repo(guarded_repo)
                    after_changes = working_tree_paths(guarded_repo)
                    if before_snapshot.head != after_snapshot.head:
                        _fail("HEAD_DRIFT", "repository HEAD changed during the role run")
                    if role in READ_ONLY_ROLES and before_snapshot != after_snapshot:
                        _fail(
                            "READ_ONLY_ROLE_MODIFIED_REPO",
                            f"read-only role {role} changed the repository",
                        )
                    if task["task_type"] == "ACCEPTANCE":
                        assert_acceptance_candidate(task, guarded_repo)
                    actual_changes = after_changes - before_changes
                    if role == "terra":
                        assert_allowed_changes(actual_changes, task["allowed_write_paths"])
                else:
                    actual_changes = set(result.get("changed_files", [])) if "result" in locals() and isinstance(result, Mapping) else set()
            metric_run = dict(result) if isinstance(result, Mapping) else {}
            metric_run.update(
                {
                    "role": role,
                    "workflow_state": state,
                    "duration_seconds": time.monotonic() - started_monotonic,
                }
            )
            record_metrics(task_id, metric_run)
            validate_role_result(role, result, actual_changes)
            validate_verification_package(role, task, result)
            return result, state
        except (WorkflowError, ValueError, json.JSONDecodeError) as exc:
            error_code = exc.code if isinstance(exc, WorkflowError) else "INVALID_ROLE_RESULT"
            _append_state_event(
                store,
                task_id,
                event_type="ROLE_FAILURE",
                previous_state=state,
                new_state=state,
                budget=budget,
                role=role,
                error_code=error_code,
            )
            if error_code in ROLE_GUARD_FAILURES:
                return None, _transition(
                    store,
                    task_id,
                    state,
                    "BLOCKED",
                    budget,
                    event_type="ROLE_GUARD_BLOCKED",
                )
            try:
                budget.consume_technical()
            except WorkflowError:
                return None, _transition(
                    store,
                    task_id,
                    state,
                    "BLOCKED",
                    budget,
                    event_type="TECHNICAL_RETRY_EXHAUSTED",
                )


def _role_state_after_result(
    store: WorkflowStore,
    task_id: str,
    state: str,
    role: str,
    result: Mapping[str, object],
    budget: RetryBudget,
) -> str:
    status = result["status"]
    if role == "luna":
        if status == "BLOCKED":
            return _transition(
                store,
                task_id,
                state,
                "BLOCKED",
                budget,
                event_type="LUNA_BLOCKED",
            )
        if status == "NOT_SUPPORTED":
            return _transition(
                store,
                task_id,
                state,
                "BLOCKED",
                budget,
                event_type="LUNA_NOT_SUPPORTED",
            )
        if status == "PARTIALLY_SUPPORTED":
            event_type = "LUNA_PARTIALLY_SUPPORTED"
        else:
            event_type = "ROLE_RESULT"
        target = "EVIDENCE_READY" if state == "EVIDENCE_RUNNING" else "PRECHECK_READY"
    elif role == "terra":
        if status == "IMPLEMENTED_CANDIDATE":
            target = "IMPLEMENTED_CANDIDATE"
        else:
            try:
                budget.consume_rework()
            except WorkflowError:
                return _transition(
                    store,
                    task_id,
                    state,
                    "BLOCKED",
                    budget,
                    event_type="IMPLEMENTATION_REWORK_EXHAUSTED",
                )
            target = "NEEDS_REPLAN"
    elif role == "sol_planner":
        target = "PLAN_READY"
    elif role == "sol_reviewer":
        target = "ESCALATION_PROPOSED" if status == "ESCALATION_PROPOSED" else "REVIEW_READY"
    elif role == "sol_xhigh":
        target = "ESCALATION_PROPOSED"
    else:
        _fail("INVALID_ROLE", f"unsupported role {role}")
    target = next_state(state, target, owner_authorized=False)
    state = _append_state_event(
        store,
        task_id,
        event_type=event_type if role == "luna" else "ROLE_RESULT",
        previous_state=state,
        new_state=target,
        budget=budget,
        role=role,
        status=status if isinstance(status, str) else None,
    )
    if state in {"PLAN_READY", "REVIEW_READY", "ESCALATION_PROPOSED", "NEEDS_REPLAN"}:
        return _transition(
            store,
            task_id,
            state,
            "AWAITING_OWNER_DECISION",
            budget,
            event_type="OWNER_GATE_REACHED",
        )
    return state


def _run_pipeline_role(
    store: WorkflowStore,
    task_id: str,
    task: dict[str, object],
    state: str,
    role: str,
    runner: Runner,
    budget: RetryBudget,
) -> str:
    result, state_after_retry = _run_role_with_technical_retry(
        store, task_id, task, state, role, runner, budget
    )
    if result is None:
        return state_after_retry
    return _role_state_after_result(store, task_id, state_after_retry, role, result, budget)


def run_until_gate(task_id: str, *, runner: Runner, allow_live_model: bool) -> str:
    """Advance one bounded pipeline only until its next owner-controlled gate."""

    if not isinstance(allow_live_model, bool):
        _fail("INVALID_LIVE_MODEL_FLAG", "allow_live_model must be a boolean")
    if not hasattr(runner, "run"):
        _fail("INVALID_RUNNER", "runner must provide run(role, task)")
    if getattr(runner, "is_live_model", False) and not allow_live_model:
        _fail("LIVE_MODEL_NOT_AUTHORIZED", "live model execution requires explicit authorization")
    store = WorkflowStore(WORKFLOW_STATE_ROOT)
    with store.lock(task_id):
        task_path = store._require_task(task_id) / "task.json"
        task = load_task(task_path)
        state = _current_state(store, task_id)
        budget = _budget_from_events(store, task_id)
        while True:
            if getattr(runner, "is_live_model", False) and task["task_type"] == "ACCEPTANCE":
                assert_acceptance_candidate(task, _execution_repo(task, "luna"))
            if state in {"BLOCKED", "CLOSED", "ABORTED", "DEFERRED", "AWAITING_OWNER_DECISION"}:
                return state
            if state == "DRAFT":
                state = _transition(store, task_id, state, "TASK_VALIDATED", budget)
                continue
            if state == "TASK_VALIDATED":
                if task["task_type"] in {"PLAN", "ACCEPTANCE"}:
                    state = _transition(store, task_id, state, "EVIDENCE_RUNNING", budget)
                    continue
                if task["risk_flags"]:
                    state = _transition(store, task_id, state, "PLAN_OR_REVIEW_RUNNING", budget)
                    continue
                state = _transition(store, task_id, state, "AWAITING_OWNER_DECISION", budget)
                continue
            if state == "EVIDENCE_RUNNING":
                state = _run_pipeline_role(store, task_id, task, state, "luna", runner, budget)
                continue
            if state == "EVIDENCE_READY":
                state = _transition(store, task_id, state, "PLAN_OR_REVIEW_RUNNING", budget)
                continue
            if state == "APPROVED_FOR_EXECUTION":
                if not _authorization_is_recorded(
                    store, task_id, state, "approve_execution"
                ):
                    return state
                if task["task_type"] == "REMEDIATION" and getattr(runner, "is_live_model", False):
                    create_worktree(task, owner_authorized=True, store=store)
                state = _transition(
                    store, task_id, state, "WORKTREE_READY", budget, owner_authorized=True
                )
                continue
            if state == "WORKTREE_READY":
                state = _transition(store, task_id, state, "IMPLEMENTATION_RUNNING", budget)
                continue
            if state == "REWORK_AUTHORIZED":
                if not _authorization_is_recorded(
                    store, task_id, state, "authorize_rework"
                ):
                    return state
                state = _transition(
                    store, task_id, state, "IMPLEMENTATION_RUNNING", budget, owner_authorized=True
                )
                continue
            if state == "IMPLEMENTATION_RUNNING":
                state = _run_pipeline_role(store, task_id, task, state, "terra", runner, budget)
                continue
            if state == "IMPLEMENTED_CANDIDATE":
                state = _transition(store, task_id, state, "PRECHECK_RUNNING", budget)
                continue
            if state == "PRECHECK_RUNNING":
                state = _run_pipeline_role(store, task_id, task, state, "luna", runner, budget)
                continue
            if state == "PRECHECK_READY":
                state = _transition(store, task_id, state, "PLAN_OR_REVIEW_RUNNING", budget)
                continue
            if state == "ESCALATION_AUTHORIZED":
                if not _authorization_is_recorded(
                    store, task_id, state, "authorize_escalation"
                ):
                    return state
                try:
                    budget.consume_escalation()
                except WorkflowError:
                    return _transition(
                        store,
                        task_id,
                        state,
                        "BLOCKED",
                        budget,
                        event_type="ESCALATION_BUDGET_EXHAUSTED",
                    )
                state = _transition(
                    store,
                    task_id,
                    state,
                    "PLAN_OR_REVIEW_RUNNING",
                    budget,
                    owner_authorized=True,
                )
                continue
            if state == "PLAN_OR_REVIEW_RUNNING":
                role = _role_for_plan_or_review(store, task_id, task)
                state = _run_pipeline_role(store, task_id, task, state, role, runner, budget)
                continue
            _fail("INVALID_STATE", f"cannot run task from state {state}")


def apply_owner_decision(task_id: str, decision: str, actor: str) -> str:
    """Append one complete owner decision and move only along its closed-set edge."""

    return _apply_owner_decision(WorkflowStore(WORKFLOW_STATE_ROOT), task_id, decision, actor)


def _apply_owner_decision(
    store: WorkflowStore, task_id: str, decision: str, actor: str
) -> str:
    """Apply one owner decision through the supplied existing workflow store."""

    if not isinstance(decision, str) or decision not in OWNER_DECISIONS:
        _fail("INVALID_OWNER_DECISION", "decision is not in the approved closed set")
    if not isinstance(actor, str) or not actor.strip():
        _fail("INVALID_ACTOR", "actor must be a non-empty string")
    with store.lock(task_id):
        store._require_task(task_id)
        state = _current_state(store, task_id)
        budget = _budget_from_events(store, task_id)
        targets = {
            "approve_execution": "APPROVED_FOR_EXECUTION",
            "authorize_rework": "REWORK_AUTHORIZED",
            "authorize_escalation": "ESCALATION_AUTHORIZED",
            "defer": "DEFERRED",
            "close": "CLOSED",
            "abort": "ABORTED",
        }
        target = targets[decision]
        if state == "DEFERRED" and decision == "approve_execution":
            target = "TASK_VALIDATED"
        try:
            target = next_state(state, target, owner_authorized=True)
        except WorkflowError as exc:
            if exc.code == "INVALID_TRANSITION":
                _fail("OWNER_DECISION_NOT_APPLICABLE", f"{decision} is not valid from {state}")
            raise
        record: dict[str, object] = {
            "event_type": "OWNER_DECISION",
            "decision": decision,
            "actor": actor.strip(),
            "timestamp_utc": _utc_timestamp(),
            "previous_state": state,
            "new_state": target,
            "task_sha256": _task_sha256(store, task_id),
        }
        store.record_decision(task_id, record)
        event = dict(record)
        event["retry_budget"] = _budget_record(budget)
        store.append_event(task_id, event)
        return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new")
    new.add_argument("task_path", nargs="?", type=Path)
    new.add_argument("--task", dest="task_option", type=Path)
    new.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    validate = sub.add_parser("validate")
    validate.add_argument("task_path", nargs="?", type=Path)
    validate.add_argument("--task", dest="task_option", type=Path)

    run = sub.add_parser("run")
    run.add_argument("task_path", nargs="?", type=Path)
    run.add_argument("--task", dest="task_option", type=Path)
    run.add_argument("--runner", choices=("fake", "live"), default=None)
    run.add_argument("--allow-live-model", action="store_true")
    run.add_argument("--role", default="luna", choices=tuple(FAKE_ROLE_RESULTS))
    run.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    status = sub.add_parser("status")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    decide = sub.add_parser("decide")
    decide.add_argument("task_id", nargs="?")
    decide.add_argument("decision", nargs="?")
    decide.add_argument("--decision", dest="decision_option")
    decide.add_argument("--by", default="owner")
    decide.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    for name in ("resume", "abort"):
        sub.add_parser(name)
    report = sub.add_parser("report")
    report.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))
    report.add_argument("--output", type=Path, required=True)
    return parser


def _task_path_from_args(args: argparse.Namespace) -> Path:
    path = args.task_option or args.task_path
    if path is None:
        raise WorkflowError("TASK_REQUIRED", "a task JSON path is required")
    return path


def _authoritative_evidence_paths(task: Mapping[str, object]) -> tuple[Path, ...]:
    """Resolve only the task's explicitly authorized evidence within its repository."""

    repository = Path(task["repository_root"]).resolve()
    evidence_paths: list[Path] = []
    for relative_path in task["authoritative_files"]:
        candidate = (repository / relative_path).resolve()
        try:
            candidate.relative_to(repository)
        except ValueError as exc:
            raise WorkflowError(
                "AUTHORITATIVE_FILE_OUTSIDE_REPO",
                "authoritative evidence must stay inside repository_root",
            ) from exc
        evidence_paths.append(candidate)
    return tuple(evidence_paths)


def _run_live_luna(task: dict[str, object], args: argparse.Namespace) -> dict:
    """Execute the explicitly authorized, first-stage Luna read-only smoke run."""

    if not args.allow_live_model:
        _fail("LIVE_MODEL_NOT_AUTHORIZED", "--allow-live-model is required for the live runner")
    if args.role != "luna":
        _fail("LIVE_ROLE_NOT_ALLOWED", "the live CLI runner is limited to luna")
    store = WorkflowStore(args.root)
    task_dir = store._require_task(task["task_id"])
    stored_task = load_task(task_dir / "task.json")
    if stored_task != task:
        _fail("TASK_STORE_MISMATCH", "live task input does not match the stored task")
    contract = {
        "acceptance_commands": task["acceptance_commands"],
        "verification_level": task["verification_level"],
    }
    repository = Path(task["repository_root"])
    paths = RunPaths(
        repo=repository,
        output_path=task_dir / "luna-result.json",
        schema_path=repository / "config" / "ai_workflow_result.schema.json",
        logs_dir=task_dir / "logs",
    )
    prompt = build_role_prompt("luna", task, contract, _authoritative_evidence_paths(task))
    return run_codex("luna", task, prompt, paths)


def _run_command(args: argparse.Namespace) -> int:
    if args.command in {"resume", "abort"}:
        raise WorkflowError("NOT_IMPLEMENTED_IN_CURRENT_STAGE", f"{args.command} is not implemented")
    if args.command == "report":
        output_path = Path(args.output)
        report = render_report(aggregate_metrics(args.root))
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report, encoding="utf-8")
        except OSError as exc:
            raise WorkflowError("REPORT_WRITE_ERROR", f"cannot write report {output_path}") from exc
        print(f"REPORT_WRITTEN {output_path}")
        return 0
    if args.command == "new":
        task = load_task(_task_path_from_args(args))
        path = WorkflowStore(args.root).create_task(task)
        print(path)
        return 0
    if args.command == "validate":
        task = load_task(_task_path_from_args(args))
        print(f"VALID {task['task_id']}")
        return 0
    if args.command == "status":
        if not args.task_id:
            raise WorkflowError("TASK_REQUIRED", "task_id is required")
        path = WorkflowStore(args.root)._require_task(args.task_id) / "task.json"
        task = load_task(path)
        print(f"PRESENT {task['task_id']}")
        return 0
    if args.command == "decide":
        if not args.task_id:
            raise WorkflowError("TASK_REQUIRED", "task_id is required")
        decision = args.decision_option or args.decision
        if not decision:
            raise WorkflowError("DECISION_REQUIRED", "decision is required")
        _apply_owner_decision(WorkflowStore(args.root), args.task_id, decision, args.by)
        print("DECISION_RECORDED")
        return 0
    if args.command == "run":
        if args.runner == "live":
            if not args.allow_live_model:
                _fail("LIVE_MODEL_NOT_AUTHORIZED", "--allow-live-model is required for the live runner")
            if args.role != "luna":
                _fail("LIVE_ROLE_NOT_ALLOWED", "the live CLI runner is limited to luna")
            task = load_task(_task_path_from_args(args))
            print(_canonical_json(_run_live_luna(task, args)))
            return 0
        if args.runner != "fake":
            raise WorkflowError("NOT_IMPLEMENTED_IN_CURRENT_STAGE", "only --runner fake is available")
        task = load_task(_task_path_from_args(args))
        print(_canonical_json(FakeRunner().run(args.role, task)))
        return 0
    raise WorkflowError("UNKNOWN_COMMAND", f"unsupported command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_command(args)
    except WorkflowError as exc:
        print(f"{exc.code}: {exc.message}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
