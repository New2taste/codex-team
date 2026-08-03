"""Deterministic validation and state transitions for the local workflow stage."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import errno
import fcntl
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


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
SAFE_ENVIRONMENT_KEYS = frozenset({"HOME", "PATH", "CODEX_HOME", "LANG", "LC_ALL", "TERM", "TMPDIR"})
CODEX_TIMEOUT_SECONDS = 120
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:TOKEN|KEY|PASSWORD|SECRET)[A-Z0-9_]*)\s*=\s*([^\s,;]+)"
)
_LONG_HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9+/=_-]{48,}(?![A-Za-z0-9_])")


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


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


def _write_role_events(log_path: Path, stdout: object) -> None:
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    text = stdout if isinstance(stdout, str) else ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(_redact_log_text(text), encoding="utf-8")


def run_codex(role: str, task: dict, prompt: str, paths: RunPaths) -> dict:
    """Run one pinned Codex role and accept only a validated output document."""

    validate_task(task)
    if not isinstance(prompt, str):
        _fail("INVALID_PROMPT", "prompt must be a string")
    before_run = capture_repo(paths.repo)
    try:
        command = build_codex_command(role, paths.repo, paths.output_path, paths.schema_path)
        events_path = Path(paths.logs_dir) / f"{role}-events.jsonl"
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                input=prompt,
                text=True,
                timeout=CODEX_TIMEOUT_SECONDS,
                env=sanitized_environment(os.environ),
                cwd=str(paths.repo),
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            _write_role_events(events_path, exc.stdout)
            raise WorkflowError("CODEX_TIMEOUT", f"{role} exceeded {CODEX_TIMEOUT_SECONDS} seconds") from exc
        _write_role_events(events_path, completed.stdout)
        if completed.returncode != 0:
            raise WorkflowError("CODEX_EXIT_NONZERO", f"{role} exited with code {completed.returncode}")
        try:
            result = json.loads(Path(paths.output_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError("INVALID_ROLE_RESULT", f"{role} did not produce valid JSON") from exc
        if not isinstance(result, dict):
            _fail("INVALID_ROLE_RESULT", "role output must be an object")
        validate_role_result(role, result, set(result.get("changed_files", [])))
        return result
    finally:
        after_run = capture_repo(paths.repo)
        if role in READ_ONLY_ROLES and before_run != after_run:
            _fail("READ_ONLY_ROLE_MODIFIED_REPO", f"read-only role {role} changed the repository")
        if before_run.head != after_run.head:
            _fail("HEAD_DRIFT", "repository HEAD changed during the role run")


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


def _has_execution_approval(repo: Path, task_id: str) -> bool:
    decision_path = repo / "data/state/ai-workflow" / task_id / "human-decisions.jsonl"
    try:
        records = decision_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in records:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("decision") == "APPROVED_FOR_EXECUTION":
            return True
    return False


def create_worktree(task: dict, owner_authorized: bool) -> Path:
    """Create the one owner-approved, task-scoped worktree without deletion support."""

    if not owner_authorized:
        _fail("OWNER_AUTHORIZATION_REQUIRED", "owner authorization is required before creating a worktree")
    validate_task(task)
    task_id = task["task_id"]
    repository_root = Path(task["repository_root"])
    if not _has_execution_approval(repository_root, task_id):
        _fail(
            "APPROVED_FOR_EXECUTION_REQUIRED",
            f"task {task_id} has no APPROVED_FOR_EXECUTION decision record",
        )
    normalized_task_id = task_id.lower()
    worktree = repository_root / ".codex-worktrees" / normalized_task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(
        repository_root,
        "worktree",
        "add",
        "-b",
        f"aiwf/{normalized_task_id}",
        str(worktree),
        "HEAD",
    )
    return worktree


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
        "CLOSED",
    }
)
TRANSITIONS = {
    "DRAFT": frozenset({"TASK_VALIDATED", "ABORTED"}),
    "TASK_VALIDATED": frozenset({"EVIDENCE_RUNNING", "BLOCKED", "ABORTED"}),
    "EVIDENCE_RUNNING": frozenset({"EVIDENCE_READY", "BLOCKED", "ABORTED"}),
    "EVIDENCE_READY": frozenset({"PLAN_OR_REVIEW_RUNNING", "BLOCKED", "ABORTED"}),
    "PLAN_OR_REVIEW_RUNNING": frozenset(
        {"PLAN_READY", "REVIEW_READY", "BLOCKED", "ESCALATION_PROPOSED"}
    ),
    "PLAN_READY": frozenset({"AWAITING_OWNER_DECISION"}),
    "REVIEW_READY": frozenset({"AWAITING_OWNER_DECISION"}),
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
}


def next_state(current: str, target: str, *, owner_authorized: bool) -> str:
    """Return ``target`` when the explicit transition is authorized."""

    if not isinstance(current, str) or not isinstance(target, str):
        _fail("INVALID_STATE", "states must be strings")
    allowed = TRANSITIONS.get(current)
    if allowed is None:
        _fail("INVALID_STATE", f"unknown current state {current}")
    if target not in allowed:
        _fail("INVALID_TRANSITION", f"cannot transition from {current} to {target}")
    if target in OWNER_ONLY_STATES and not owner_authorized:
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
        return {
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
    run.add_argument("--runner", default=None)
    run.add_argument("--role", default="luna", choices=tuple(FAKE_ROLE_RESULTS))

    status = sub.add_parser("status")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    decide = sub.add_parser("decide")
    decide.add_argument("task_id", nargs="?")
    decide.add_argument("decision", nargs="?")
    decide.add_argument("--decision", dest="decision_option")
    decide.add_argument("--by", default="owner")
    decide.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    for name in ("resume", "abort", "report"):
        sub.add_parser(name)
    return parser


def _task_path_from_args(args: argparse.Namespace) -> Path:
    path = args.task_option or args.task_path
    if path is None:
        raise WorkflowError("TASK_REQUIRED", "a task JSON path is required")
    return path


def _run_command(args: argparse.Namespace) -> int:
    if args.command in {"resume", "abort", "report"}:
        raise WorkflowError("NOT_IMPLEMENTED_IN_CURRENT_STAGE", f"{args.command} is not implemented")
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
        WorkflowStore(args.root).record_decision(
            args.task_id, {"decision": decision, "by": args.by}
        )
        print("DECISION_RECORDED")
        return 0
    if args.command == "run":
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
