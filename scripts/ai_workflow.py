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
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, Sequence


if __name__ == "__main__":
    # Keep direct-script imports on the same public exception/module object.
    sys.modules.setdefault("ai_workflow", sys.modules[__name__])


TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "paired_case_id",
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
REQUIRED_TASK_FIELDS = TASK_FIELDS - {"paired_case_id"}
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
ATTEMPT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
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
RESULT_IDENTITY_FIELDS = frozenset({"dispatch_id", "task_id", "step_id", "attempt"})
READ_ONLY_ROLES = frozenset(
    {
        "luna",
        "sol_planner",
        "sol_reviewer",
        "sol_xhigh",
        "sol_medium_supervisor",
        "sol_medium_reviewer",
        "sol_xhigh_planner",
        "terra_xhigh_planner",
        "terra_xhigh_reviewer",
    }
)
TERRA_WRITE_ROLES = frozenset({"luna_construction", "terra", "terra_xhigh"})
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


try:
    from .ai_workflow_artifacts import (
        ArtifactError,
        CostEvidence,
        PlanArtifact,
        RouteAdvice,
        RouteDecision,
        RouteRequest,
        RuntimeEvidence,
        artifact_sha256,
        load_artifact,
        validate_cost_evidence,
        validate_plan_shape,
        validate_route_advice,
        validate_route_decision,
        validate_route_request,
        validate_runtime_evidence,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ArtifactError,
        CostEvidence,
        PlanArtifact,
        RouteAdvice,
        RouteDecision,
        RouteRequest,
        RuntimeEvidence,
        artifact_sha256,
        load_artifact,
        validate_cost_evidence,
        validate_plan_shape,
        validate_route_advice,
        validate_route_decision,
        validate_route_request,
        validate_runtime_evidence,
    )

try:
    from .ai_workflow_runtime import (
        CODEX_EXEC_ROLE_CONTRACT,
        NATIVE_SUBAGENT,
        RuntimeArtifactSnapshot,
        RuntimeObservation,
        RuntimeRepositorySnapshot,
        codex_exec_contract,
        codex_exec_observation,
        extract_codex_thread_id,
        extract_codex_usage,
        inspect_agent_runtime,
        merge_runtime_observations,
        parse_codex_jsonl,
        permission_is_within_contract,
        runtime_artifact_snapshot,
        runtime_repository_snapshot,
        verify_runtime_identity,
        write_runtime_evidence,
    )
except ImportError:  # direct script execution
    from ai_workflow_runtime import (
        CODEX_EXEC_ROLE_CONTRACT,
        NATIVE_SUBAGENT,
        RuntimeArtifactSnapshot,
        RuntimeObservation,
        RuntimeRepositorySnapshot,
        codex_exec_contract,
        codex_exec_observation,
        extract_codex_thread_id,
        extract_codex_usage,
        inspect_agent_runtime,
        merge_runtime_observations,
        parse_codex_jsonl,
        permission_is_within_contract,
        runtime_artifact_snapshot,
        runtime_repository_snapshot,
        verify_runtime_identity,
        write_runtime_evidence,
    )

try:
    from .ai_workflow_costs import (
        aggregate_paired_cases,
        evaluate_cost_claim,
        evaluate_optimization_gate,
        finite_nonnegative_or_none,
        normalize_cost_evidence,
        render_cost_sections,
    )
except ImportError:  # direct script execution
    from ai_workflow_costs import (
        aggregate_paired_cases,
        evaluate_cost_claim,
        evaluate_optimization_gate,
        finite_nonnegative_or_none,
        normalize_cost_evidence,
        render_cost_sections,
    )


try:
    from .ai_workflow_team_call import (
        L0_FIXED_ARGV,
        TeamCallError,
        TeamCallReceipt,
        TeamCallRegistry,
        TeamCallRoute,
        classify_team_call,
        parse_team_call,
    )
except ImportError:  # direct script execution
    from ai_workflow_team_call import (
        L0_FIXED_ARGV,
        TeamCallError,
        TeamCallReceipt,
        TeamCallRegistry,
        TeamCallRoute,
        classify_team_call,
        parse_team_call,
    )


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
    if role in TERRA_WRITE_ROLES:
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


def _load_workflow_config() -> dict[str, object]:
    """Load the one pinned workflow configuration document."""

    try:
        import tomllib

        with ROLE_CONFIG_PATH.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowError("INVALID_ROLE", "workflow configuration is unreadable") from exc
    if not isinstance(config, dict):
        raise WorkflowError("INVALID_ROLE", "workflow configuration must be an object")
    return config


def _load_role_config(role: str) -> dict[str, object]:
    """Return the pinned configuration for one named workflow role."""

    try:
        role_config = _load_workflow_config()["roles"][role]
    except (KeyError, TypeError) as exc:
        raise WorkflowError("INVALID_ROLE", f"unsupported or unreadable role {role}") from exc
    if not isinstance(role_config, dict):
        raise WorkflowError("INVALID_ROLE", f"unsupported role {role}")
    return role_config


_COMPACT_GATE_TOKEN = object()


@dataclass(frozen=True)
class PromptBuildResult:
    """Deterministic prompt render: full or compact projection, never a summary."""

    prompt: str
    mode: str
    reason: str
    prompt_bytes: int
    _gate_token: object = field(default=None, repr=False, compare=False)


TASK_COMPACT_REQUIRED_FIELDS = (
    "task_id",
    "schema_version",
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
)
TASK_COMPACT_OPTIONAL_FIELDS = ("paired_case_id",)
EVIDENCE_AUTHORIZATION_SENTENCES = (
    "Read the named evidence files at the listed paths before evaluating the task.",
    "Use only the task contract and named evidence above; no additional source material is authorized.",
)
CONTRACT_COMPACT_REQUIRED_FIELDS = (
    "schema_version",
    "role",
    "dispatch_id",
    "plan_id",
    "plan_sha256",
    "task_sha256",
    "request_sha256",
    "subtask_id",
    "step_id",
    "write_scope",
    "read_scope",
    "do_not_touch",
    "acceptance_criteria",
    "acceptance",
    "acceptance_commands",
    "verification_commands",
    "verification_level",
    "dependencies",
    "depends_on",
    "permission_profile",
    "candidate_sha256",
    "candidate_commit",
    "evidence_sha256",
    "first_artifact",
    "construction_envelope",
    "runtime_session_id",
    "session_id",
    "native_agent_id",
    "native_thread_id",
    "owner_decision",
    "owner_decisions",
    "authorization_ticket",
    "authorization_tickets",
    "output_schema",
    "output_schema_path",
    "output_path",
    "required_output_schema",
    "required_output_path",
    "team_call_attestation",
)


def resolve_compact_prompt_decision(
    *,
    config: object = None,
    metrics: object = None,
) -> tuple[bool, str]:
    """Arm compact prompts only from validated config plus the data gate."""

    if config is None:
        try:
            config = _load_workflow_config()
        except Exception:
            return False, "config_unavailable"
    try:
        policy = resolve_optimization_policy(config)
    except Exception:
        return False, "policy_invalid"
    if policy.mode != "enforced":
        return False, "mode_not_enforced"
    if policy.compact_prompts is not True:
        return False, "compact_flag_false"
    if not isinstance(metrics, Mapping):
        return False, "metrics_missing"
    if metrics.get("synthetic") is True:
        return False, "synthetic"
    try:
        gate = evaluate_optimization_gate(
            metrics,
            minimum_cases=policy.minimum_paired_cases,
        )
    except Exception:
        return False, "metrics_invalid"
    if gate != "ALLOW_ENFORCED":
        return False, "gate_not_armed"
    return True, "armed"


def _named_evidence(evidence_paths: Sequence[Path]) -> list[dict[str, str]]:
    evidence = []
    for evidence_path in evidence_paths:
        path = Path(evidence_path)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise WorkflowError("EVIDENCE_READ_ERROR", f"cannot read evidence {path}") from exc
        evidence.append({"path": str(path), "sha256": digest})
    return evidence


def _role_prompt_suffix(
    role: str,
    role_config: Mapping[str, object],
    task: Mapping[str, object],
) -> tuple[str, ...]:
    return (
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
        *(
            (
                "Do not write, modify, delete, stage, commit, merge, or push repository files.",
            )
            if role in READ_ONLY_ROLES
            else ()
        ),
        *EVIDENCE_AUTHORIZATION_SENTENCES,
    )


def _render_full_role_prompt(
    role: str,
    role_config: Mapping[str, object],
    task: Mapping[str, object],
    contract: Mapping[str, object],
    evidence: Sequence[Mapping[str, str]],
) -> str:
    return "\n".join(
        (
            f"Role instructions: {role_config['instructions']}",
            f"Task envelope: {_canonical_json(dict(task))}",
            f"Task contract: {_canonical_json(dict(contract))}",
            f"Named evidence: {_canonical_json(list(evidence))}",
            *_role_prompt_suffix(role, role_config, task),
            "only output ai-result-1 JSON",
        )
    )


def _project_compact_context(
    task: Mapping[str, object],
    contract: Mapping[str, object],
) -> dict[str, object] | None:
    projected: dict[str, object] = {}
    for key in TASK_COMPACT_REQUIRED_FIELDS:
        if key in task:
            projected[key] = task[key]
    for key in TASK_COMPACT_OPTIONAL_FIELDS:
        if key in task:
            projected[key] = task[key]
    for key, value in contract.items():
        if not isinstance(key, str):
            return None
        if key in projected and projected[key] == value:
            continue
        projected[f"contract.{key}" if key in projected else key] = value
    return projected


def _canonical_equal(left: object, right: object) -> bool:
    try:
        return _canonical_json(left) == _canonical_json(right)
    except WorkflowError:
        return False


def _read_compact_context(prompt: str) -> dict[str, object] | None:
    for line in prompt.splitlines():
        if not line.startswith("Context: "):
            continue
        try:
            value = json.loads(line.removeprefix("Context: "))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _compact_projection_is_faithful(
    prompt: str,
    role: str,
    role_config: Mapping[str, object],
    task: Mapping[str, object],
    contract: Mapping[str, object],
) -> bool:
    if str(role_config["instructions"]) not in prompt:
        return False
    if f'Output "role" exactly as "{role}".' not in prompt:
        return False
    if any(sentence not in prompt for sentence in EVIDENCE_AUTHORIZATION_SENTENCES):
        return False
    expected = _project_compact_context(task, contract)
    actual = _read_compact_context(prompt)
    if expected is None or actual is None or set(actual) != set(expected):
        return False
    return all(_canonical_equal(actual[key], value) for key, value in expected.items())


def _render_compact_role_prompt(
    role: str,
    role_config: Mapping[str, object],
    task: Mapping[str, object],
    contract: Mapping[str, object],
    evidence: Sequence[Mapping[str, str]],
) -> str | None:
    projected = _project_compact_context(task, contract)
    if projected is None:
        return None
    try:
        context_json = _canonical_json(projected)
        parsed = json.loads(context_json)
        evidence_json = _canonical_json(list(evidence))
    except (WorkflowError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != set(projected):
        return None
    if any(not _canonical_equal(parsed[key], value) for key, value in projected.items()):
        return None
    prompt = "\n".join(
        (
            f"Role instructions: {role_config['instructions']}",
            f"Context: {context_json}",
            f"Named evidence: {evidence_json}",
            *_role_prompt_suffix(role, role_config, task),
            "only output ai-result-1 JSON",
        )
    )
    if not _compact_projection_is_faithful(prompt, role, role_config, task, contract):
        return None
    return prompt


def _builder_compact_decision(state_root: Path | None) -> tuple[bool, str]:
    try:
        config = _load_workflow_config()
    except Exception:
        return False, "config_unavailable"
    metrics = None
    if state_root is not None:
        try:
            metrics = aggregate_metrics(Path(state_root))
        except Exception:
            return False, "metrics_invalid"
    return resolve_compact_prompt_decision(config=config, metrics=metrics)


def build_role_prompt_result(
    role: str,
    task: Mapping[str, object],
    contract: Mapping[str, object],
    evidence_paths: Sequence[Path],
    *,
    state_root: Path | None = None,
) -> PromptBuildResult:
    """Build a full or compact role prompt from pinned config and state metrics."""

    role_config = _load_role_config(role)
    validate_task(task)
    if not isinstance(contract, Mapping):
        _fail("INVALID_CONTRACT", "contract must be an object")
    evidence = _named_evidence(evidence_paths)
    full_prompt = _render_full_role_prompt(role, role_config, task, contract, evidence)
    armed, reason = _builder_compact_decision(state_root)
    if armed:
        compact_prompt = _render_compact_role_prompt(
            role, role_config, task, contract, evidence
        )
        if compact_prompt is not None:
            compact_bytes = len(compact_prompt.encode("utf-8"))
            if compact_bytes < len(full_prompt.encode("utf-8")):
                return PromptBuildResult(
                    prompt=compact_prompt,
                    mode="compact",
                    reason=reason,
                    prompt_bytes=compact_bytes,
                    _gate_token=_COMPACT_GATE_TOKEN,
                )
            reason = "compact_not_smaller"
        else:
            reason = "compact_unfaithful"
    return PromptBuildResult(
        prompt=full_prompt,
        mode="full",
        reason=reason,
        prompt_bytes=len(full_prompt.encode("utf-8")),
    )


def build_role_prompt(
    role: str,
    task: Mapping[str, object],
    contract: Mapping[str, object],
    evidence_paths: Sequence[Path],
    *,
    state_root: Path | None = None,
) -> str:
    """Build the bounded prompt from only the supplied task, contract, and evidence."""

    return build_role_prompt_result(
        role,
        task,
        contract,
        evidence_paths,
        state_root=state_root,
    ).prompt


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
    runtime_evidence_required: bool = False
    runtime_sessions_dir: Path | None = None


@dataclass(frozen=True)
class AttemptAccountingContext:
    """Controller-issued identity and retry label for one live Codex attempt."""

    task_id: str
    role: str
    retry_kind: str
    attempt_id: str


def _new_attempt_accounting_context(
    task_id: str, role: str, retry_kind: str
) -> AttemptAccountingContext:
    if retry_kind not in {"none", "technical"}:
        _fail("INVALID_RETRY_KIND", "attempt retry kind is not supported")
    return AttemptAccountingContext(
        task_id=task_id,
        role=role,
        retry_kind=retry_kind,
        attempt_id=f"{role}-{time.time_ns()}-{uuid.uuid4().hex}",
    )


def _require_attempt_accounting_context(
    value: AttemptAccountingContext | None, task_id: str, role: str
) -> AttemptAccountingContext:
    context = value or _new_attempt_accounting_context(task_id, role, "none")
    if (
        not isinstance(context, AttemptAccountingContext)
        or not isinstance(context.task_id, str)
        or not TASK_ID_PATTERN.fullmatch(context.task_id)
        or not isinstance(context.role, str)
        or not context.role.strip()
        or not isinstance(context.attempt_id, str)
        or not ATTEMPT_ID_PATTERN.fullmatch(context.attempt_id)
        or context.retry_kind not in {"none", "technical"}
    ):
        _fail("INVALID_ATTEMPT_CONTEXT", "attempt accounting context is invalid")
    if context.task_id != task_id or context.role != role:
        _fail("ATTEMPT_CONTEXT_MISMATCH", "attempt accounting context does not match this role task")
    return context


def _claim_attempt_context(paths: RunPaths, context: AttemptAccountingContext) -> None:
    """Atomically reserve one controller-issued attempt identity without replay cleanup."""

    if paths.state_root is None:
        task_dir = Path(paths.output_path).parent
    else:
        task_dir = WorkflowStore(paths.state_root)._require_task(context.task_id)
    claims_dir = task_dir / "attempt-claims"
    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
        if not claims_dir.is_dir() or claims_dir.is_symlink():
            _fail("ATTEMPT_CLAIM_DIRECTORY_INVALID", "attempt claim directory is not controlled")
    except OSError as exc:
        raise WorkflowError(
            "ATTEMPT_CLAIM_DIRECTORY_INVALID", "cannot create attempt claim directory"
        ) from exc
    claim_path = claims_dir / f"{context.attempt_id}.json"
    identity = {
        "task_id": context.task_id,
        "role": context.role,
        "retry_kind": context.retry_kind,
        "attempt_id": context.attempt_id,
    }
    try:
        descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _fail("ATTEMPT_CONTEXT_REUSED", "attempt accounting context has already been claimed")
    except OSError as exc:
        raise WorkflowError("ATTEMPT_CLAIM_FAILED", "cannot claim attempt context") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json({"schema_version": "attempt-claim-1", "identity": identity}))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkflowError("ATTEMPT_CLAIM_FAILED", "cannot persist attempt claim") from exc


def _verified_cost_route(state_root: Path | None, task_id: str) -> str:
    """Read one persisted route decision, failing closed when none is present."""

    if state_root is None:
        return "blocked"
    try:
        task_dir = WorkflowStore(state_root)._require_task(task_id)
        decision = load_artifact(task_dir / "route-decision.json")
        validate_route_decision(decision)
        if decision.get("task_id") != task_id:
            return "blocked"
    except (WorkflowError, ArtifactError, OSError, json.JSONDecodeError):
        return "blocked"
    route_value = decision.get("route")
    return route_value if isinstance(route_value, str) and route_value in {"direct", "sol_only", "delegated", "blocked"} else "blocked"


def _task_paired_case_id(task: Mapping[str, object]) -> str | None:
    """Use only an explicitly registered pair id; never derive one from task id."""

    value = task.get("paired_case_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _literal_runtime_usage(value: object) -> dict[str, int | None]:
    """Keep only explicit, finite non-negative integer runtime usage values."""

    usage: dict[str, int | None] = {}
    for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
        candidate = value.get(field) if isinstance(value, Mapping) else None
        usage[field] = (
            candidate
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0
            else None
        )
    return usage


def _controller_cost_attempt(
    task_id: str,
    task: Mapping[str, object],
    role: str,
    execution_surface: str,
    duration_seconds: float,
    prompt_bytes: int,
    runtime_usage: object,
    retry_kind: str,
    quality_outcome: str,
    state_root: Path | None,
    *,
    attempt_id: str | None = None,
    verification_seconds: float = 0.0,
    base_metric_run: Mapping[str, object] | None = None,
    compact_applied: bool = False,
) -> None:
    """Append controller-owned cost evidence for one success or failed attempt."""

    if state_root is None:
        return
    usage = _literal_runtime_usage(runtime_usage)
    evidence_class = "measured" if any(value is not None for value in usage.values()) else "unavailable"
    metric_run = dict(base_metric_run) if isinstance(base_metric_run, Mapping) else {}
    metric_run.update({
        "role": role,
        "status": quality_outcome,
        "duration_seconds": duration_seconds,
        "compact_applied": compact_applied is True,
    })
    metric_run["cost_evidence"] = {
        "schema_version": "cost-evidence-1",
        "route": _verified_cost_route(state_root, task_id),
        "role": role,
        "execution_surface": execution_surface,
        "duration_seconds": duration_seconds,
        "prompt_bytes": prompt_bytes,
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "retry_kind": retry_kind,
        "verification_seconds": verification_seconds,
        "quality_outcome": quality_outcome,
        "paired_case_id": _task_paired_case_id(task),
        "evidence_class": evidence_class,
        "rate_snapshot_id": None,
        "evidence_origin": "production",
    }
    if attempt_id is not None:
        metric_run["cost_evidence"]["attempt_id"] = attempt_id
    _record_controller_metrics(
        task_id,
        metric_run,
        state_root=state_root,
    )


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
    identity_fields = fields & RESULT_IDENTITY_FIELDS
    unknown = sorted(fields - RESULT_REQUIRED_FIELDS - RESULT_IDENTITY_FIELDS)
    if identity_fields and identity_fields != RESULT_IDENTITY_FIELDS:
        missing_identity = sorted(RESULT_IDENTITY_FIELDS - identity_fields)[0]
        _fail("INVALID_ROLE_RESULT", f"unexpected result field {missing_identity}")
    if missing or unknown:
        field = missing[0] if missing else unknown[0]
        _fail("INVALID_ROLE_RESULT", f"unexpected result field {field}")
    if result["schema_version"] != "ai-result-1":
        _fail("INVALID_ROLE_RESULT", "schema_version must be ai-result-1")
    if identity_fields:
        if (
            not isinstance(result["dispatch_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["dispatch_id"])
            or not isinstance(result["task_id"], str)
            or not result["task_id"]
            or not isinstance(result["step_id"], str)
            or not result["step_id"]
            or not isinstance(result["attempt"], int)
            or isinstance(result["attempt"], bool)
            or result["attempt"] < 1
        ):
            _fail("INVALID_ROLE_RESULT", "scheduler result identity is invalid")
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


def _construction_evidence_observation(check: ConstructionCheck) -> str:
    """Canonical observation text for a frozen construction check."""

    if not isinstance(check, ConstructionCheck):
        _fail("INVALID_VERIFICATION_PACKAGE", "construction check is invalid")
    if check.kind == "HASH":
        return f"sha256={check.sha256}"
    return (
        f"command={check.command}; expected_exit={check.expected_exit}; "
        f"assertion={check.assertion}"
    )


def _safe_scope_identity(repository: Path, scope: str) -> tuple[tuple[int, int, int], ...]:
    """Resolve existing scope components with O_NOFOLLOW and return identities."""

    root = Path(repository)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise WorkflowError("CONSTRUCTION_SCOPE_UNSAFE", "repository root is not safely openable") from exc
    root_stat = os.fstat(root_fd)
    identities: list[tuple[int, int, int]] = [
        (root_stat.st_dev, root_stat.st_ino, root_stat.st_mode)
    ]
    current_fd = root_fd
    parts = normalize_scope(scope).parts
    try:
        for index, component in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise WorkflowError(
                    "CONSTRUCTION_SCOPE_UNSAFE", f"unsafe scope component: {scope}"
                ) from exc
            stat_result = os.fstat(next_fd)
            identities.append((stat_result.st_dev, stat_result.st_ino, stat_result.st_mode))
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return tuple(identities)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _safe_artifact_sha256(repository: Path, artifact: str) -> str:
    """Hash one regular artifact through a no-follow descriptor."""

    target = normalize_scope(artifact)
    root_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current_fd = root_fd
    try:
        for component in target.parts[:-1]:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(target.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        try:
            stat_before = os.fstat(file_fd)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_fd, 65536)
                if not chunk:
                    break
                digest.update(chunk)
            stat_after = os.fstat(file_fd)
            if (stat_before.st_dev, stat_before.st_ino, stat_before.st_size, stat_before.st_mtime_ns) != (
                stat_after.st_dev, stat_after.st_ino, stat_after.st_size, stat_after.st_mtime_ns
            ):
                _fail("CONSTRUCTION_EVIDENCE_DRIFT", "artifact changed while it was hashed")
            return digest.hexdigest()
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise WorkflowError("CONSTRUCTION_EVIDENCE_FAILED", f"cannot hash artifact {artifact}") from exc
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _execute_construction_command(repository: Path, check: ConstructionCheck) -> dict[str, object]:
    argv = list(
        construction_evidence_argv(check, error_code="CONSTRUCTION_EVIDENCE_FAILED")
    )
    try:
        completed = subprocess.run(
            argv,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repository)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("CONSTRUCTION_EVIDENCE_FAILED", "evidence argv could not complete") from exc
    output = completed.stdout[-65536:]
    observation = {
        "source": "controller",
        "argv": argv,
        "exit_code": completed.returncode,
        "output": output,
    }
    if completed.returncode != check.expected_exit:
        _fail("CONSTRUCTION_EVIDENCE_FAILED", "evidence command returned an unexpected exit")
    if check.assertion not in output and str(check.assertion) != f"exit={completed.returncode}":
        _fail("CONSTRUCTION_EVIDENCE_FAILED", "evidence assertion was not observed")
    return observation


def _bind_controller_construction_evidence(
    result: Mapping[str, object], task: Mapping[str, object], context: ConstructionExecutionContext
) -> dict[str, object]:
    """Replace model attestations with evidence observed by the controller."""

    if context.role != "luna_construction" or context.step.construction_envelope is None:
        return dict(result)
    repository = Path(task.get("source_worktree") or task["repository_root"])
    scopes = set(context.step.read_scope) | set(context.step.write_scope)
    before = {scope: _safe_scope_identity(repository, scope) for scope in scopes}
    envelope = context.step.construction_envelope
    checks = dict(envelope.evidence)
    evidence: list[dict[str, object]] = []
    for level in ("L0", "L1", "L2"):
        check = checks[level]
        if level == "L0":
            actual_hash = _safe_artifact_sha256(repository, check.artifact)
            if actual_hash != check.sha256:
                _fail("CONSTRUCTION_EVIDENCE_FAILED", "L0 artifact hash does not match the frozen digest")
            observed: dict[str, object] = {
                "source": "controller", "sha256": actual_hash,
                "device_inode": list(_safe_scope_identity(repository, check.artifact)[-1][:2]),
            }
        else:
            observed = _execute_construction_command(repository, check)
        evidence.append(
            {
                "id": level,
                "type": check.kind,
                "locator": check.artifact,
                "observation": _canonical_json(observed),
            }
        )
    negative = envelope.negative_checks[0]
    negative_observed = _execute_construction_command(repository, negative)
    after = {scope: _safe_scope_identity(repository, scope) for scope in scopes}
    if before != after:
        _fail("CONSTRUCTION_SCOPE_DRIFT", "scope identity changed while evidence was collected")
    bound = dict(result)
    bound["evidence"] = evidence
    claims = bound.get("claims")
    if not isinstance(claims, list) or len(claims) != 1:
        _fail("INVALID_VERIFICATION_PACKAGE", "construction result must reference controller evidence")
    bound["counter_checks"] = [
        {
            "target_claim_id": claims[0].get("id"),
            "method": str(negative.command),
            "result": _canonical_json(negative_observed),
        }
    ]
    return bound


def _validate_luna_construction_verification(
    result: Mapping[str, object], step: FrozenSubtask | None
) -> None:
    """Require a candidate result to reproduce all frozen L0/L1/L2 checks."""

    if step is None or step.construction_envelope is None:
        _fail(
            "INVALID_VERIFICATION_PACKAGE",
            "luna construction result has no frozen verification envelope",
        )
    if result["status"] != "IMPLEMENTED_CANDIDATE":
        return
    envelope = step.construction_envelope
    evidence_by_level = dict(envelope.evidence)
    expected = {
        "L0": evidence_by_level["L0"],
        "L1": evidence_by_level["L1"],
        "L2": evidence_by_level["L2"],
    }
    evidence = result["evidence"]
    evidence_by_id = {entry["id"]: entry for entry in evidence}
    if set(evidence_by_id) != {"L0", "L1", "L2"}:
        _fail("INVALID_VERIFICATION_PACKAGE", "luna construction requires exactly L0/L1/L2 evidence")
    expected_types = {"L0": "HASH", "L1": "COMMAND", "L2": "TEST"}
    for level, check in expected.items():
        record = evidence_by_id[level]
        if (
            record["type"] != expected_types[level]
            or record["locator"] != check.artifact
            or not isinstance(record["observation"], str)
        ):
            _fail(
                "INVALID_VERIFICATION_PACKAGE",
                f"luna construction {level} evidence does not match the frozen contract",
            )
    claims = result["claims"]
    if len(claims) != 1 or set(claims[0]["evidence_ids"]) != {"L0", "L1", "L2"}:
        _fail("INVALID_VERIFICATION_PACKAGE", "luna construction candidate must bind one claim to L0/L1/L2")
    counter_checks = result["counter_checks"]
    if len(counter_checks) != 1 or counter_checks[0]["target_claim_id"] != claims[0]["id"]:
        _fail("INVALID_VERIFICATION_PACKAGE", "luna construction requires one bound negative check")
    negative = envelope.negative_checks
    if not any(counter_checks[0]["method"] == check.command for check in negative):
        _fail("INVALID_VERIFICATION_PACKAGE", "luna construction negative check is not frozen")
    for record in evidence:
        try:
            observation = json.loads(record["observation"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkflowError("INVALID_VERIFICATION_PACKAGE", "construction evidence is not controller JSON") from exc
        if observation.get("source") != "controller":
            _fail("INVALID_VERIFICATION_PACKAGE", "construction evidence is not controller-produced")
    try:
        negative_observation = json.loads(counter_checks[0]["result"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowError("INVALID_VERIFICATION_PACKAGE", "negative evidence is not controller JSON") from exc
    if negative_observation.get("source") != "controller":
        _fail("INVALID_VERIFICATION_PACKAGE", "negative evidence is not controller-produced")


def validate_verification_package(
    role: str,
    task: Mapping[str, object],
    result: Mapping[str, object],
    *,
    construction_step: FrozenSubtask | None = None,
) -> None:
    """Enforce only the mechanically decidable parts of an evidence level."""

    validate_task(task)
    if role == "luna_construction":
        _validate_luna_construction_verification(result, construction_step)
        return
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


def _require_runtime_sessions_directory(value: Path | None) -> Path:
    """Require an explicit, usable local session root before live execution."""

    if value is None:
        _fail("RUNTIME_EVIDENCE_MISSING", "an absolute runtime sessions directory is required")
    if not isinstance(value, Path) or not value.is_absolute():
        _fail(
            "RUNTIME_EVIDENCE_INVALID",
            "runtime sessions directory must be an absolute existing directory",
        )
    try:
        if not value.is_dir():
            _fail(
                "RUNTIME_EVIDENCE_INVALID",
                "runtime sessions directory must be an absolute existing directory",
            )
    except OSError as exc:
        raise WorkflowError(
            "RUNTIME_EVIDENCE_INVALID",
            "runtime sessions directory cannot be inspected",
        ) from exc
    return value


def _construction_prompt_candidates(
    task: Mapping[str, object],
    context: ConstructionExecutionContext,
) -> tuple[str, str | None]:
    """Render legal full/compact contract candidates without reading the gate."""

    role_config = _load_role_config(context.role)
    contract = context.contract()
    full_prompt = _render_full_role_prompt(context.role, role_config, task, contract, ())
    compact_prompt = _render_compact_role_prompt(
        context.role, role_config, task, contract, ()
    )
    if compact_prompt is None:
        return full_prompt, None
    if len(compact_prompt.encode("utf-8")) >= len(full_prompt.encode("utf-8")):
        return full_prompt, None
    return full_prompt, compact_prompt


def _reconcile_construction_prompt(
    task: Mapping[str, object],
    context: ConstructionExecutionContext,
    prompt: str,
    prompt_result: PromptBuildResult | None,
) -> str:
    """Require the prompt to equal a frozen contract candidate, never a later gate."""

    full_prompt, compact_prompt = _construction_prompt_candidates(task, context)
    if prompt == full_prompt:
        matched = "full"
    elif (
        compact_prompt is not None
        and prompt == compact_prompt
        and prompt_result is not None
        and prompt_result._gate_token is _COMPACT_GATE_TOKEN
    ):
        matched = "compact"
    else:
        _fail(
            "CONSTRUCTION_PROMPT_MISMATCH",
            "construction prompt must be generated from the frozen contract",
        )
    if prompt_result is not None and prompt_result.mode != matched:
        _fail(
            "CONSTRUCTION_PROMPT_MISMATCH",
            "construction prompt result mode does not match the frozen contract candidate",
        )
    return matched


def _require_self_consistent_prompt_result(
    prompt: str,
    prompt_result: PromptBuildResult | None,
) -> PromptBuildResult | None:
    """Accept only a self-consistent builder result; forged compact metadata is rejected."""

    if prompt_result is None:
        return None
    if not isinstance(prompt_result, PromptBuildResult) or prompt_result.prompt != prompt:
        _fail("INVALID_PROMPT", "prompt result does not match the supplied prompt")
    if prompt_result.prompt_bytes != len(prompt.encode("utf-8")):
        _fail("INVALID_PROMPT", "prompt result bytes do not match the prompt")
    if prompt_result.mode not in {"full", "compact"}:
        _fail("INVALID_PROMPT", "prompt result mode is invalid")
    if prompt_result.mode == "compact":
        if (
            prompt_result.reason != "armed"
            or prompt_result._gate_token is not _COMPACT_GATE_TOKEN
        ):
            _fail("INVALID_PROMPT", "compact prompt result reason is invalid")
        if "Context: " not in prompt or "Task envelope:" in prompt:
            _fail("INVALID_PROMPT", "compact prompt result is not a compact projection")
        if any(sentence not in prompt for sentence in EVIDENCE_AUTHORIZATION_SENTENCES):
            _fail("INVALID_PROMPT", "compact prompt result dropped evidence authorization")
    elif "Task envelope:" not in prompt:
        _fail("INVALID_PROMPT", "full prompt result is not a full prompt")
    return prompt_result


def run_codex(
    role: str,
    task: dict,
    prompt: str,
    paths: RunPaths,
    *,
    attempt_context: AttemptAccountingContext | None = None,
    construction_plan: object | None = None,
    construction_step_id: object = None,
    construction_context: ConstructionExecutionContext | None = None,
    prompt_result: PromptBuildResult | None = None,
) -> dict:
    """Run one pinned Codex role and accept only a validated output document."""

    validate_task(task)
    if not isinstance(prompt, str):
        _fail("INVALID_PROMPT", "prompt must be a string")
    accepted_prompt_result = _require_self_consistent_prompt_result(prompt, prompt_result)
    luna_construction_step: FrozenSubtask | None = None
    if role == "luna_construction":
        if task["task_type"] != "REMEDIATION" or task["risk_flags"]:
            _fail(
                "LUNA_ENVELOPE_INVALID",
                "luna construction is limited to low-risk remediation work",
            )
        if not isinstance(construction_context, ConstructionExecutionContext):
            _fail(
                "LUNA_ENVELOPE_INVALID",
                "luna construction requires a hash-bound frozen dispatch context",
            )
        luna_construction_step = require_luna_construction_step(
            construction_plan, task, construction_step_id
        )
        if (
            construction_context.role != role
            or construction_context.step != luna_construction_step
            or construction_context.plan.plan_sha256
            != validate_plan(construction_plan, task).plan_sha256
        ):
            _fail("LUNA_ENVELOPE_INVALID", "luna construction context does not bind the supplied step")
    if construction_context is not None and role in {"luna_construction", "terra_xhigh"}:
        _reconcile_construction_prompt(
            task,
            construction_context,
            prompt,
            accepted_prompt_result,
        )
    accounting_context = _require_attempt_accounting_context(
        attempt_context, task["task_id"], role
    )
    runtime_sessions_dir: Path | None = None
    if paths.runtime_evidence_required:
        runtime_sessions_dir = _require_runtime_sessions_directory(paths.runtime_sessions_dir)
    repo = Path(paths.repo).resolve()
    if repo != _execution_repo(task, role):
        _fail("ROLE_REPOSITORY_MISMATCH", "role repository does not match the task execution repository")
    if role in TERRA_WRITE_ROLES:
        _assert_terra_worktree_authorized(task, repo, paths.state_root)
    if role in READ_ONLY_ROLES:
        _reject_dirty_input(repo, "DIRTY_READ_ONLY_REPOSITORY", "read-only role requires a clean repository")
    if task["task_type"] == "ACCEPTANCE":
        _reject_dirty_input(repo, "DIRTY_ACCEPTANCE_REPOSITORY", "acceptance requires a clean repository")
        assert_acceptance_candidate(task, repo)
    if role in TERRA_WRITE_ROLES:
        _reject_dirty_input(repo, "DIRTY_TERRA_WORKTREE", "Terra requires a clean source_worktree")
    _claim_attempt_context(paths, accounting_context)
    runtime_store: WorkflowStore | None = None
    runtime_task_dir: Path | None = None
    runtime_before_artifacts: RuntimeArtifactSnapshot | None = None
    if paths.runtime_evidence_required:
        if paths.state_root is None:
            _fail("RUNTIME_EVIDENCE_MISSING", "live execution requires a workflow state root")
        runtime_store = WorkflowStore(paths.state_root)
        runtime_task_dir = runtime_store._require_task(task["task_id"])
        # A new attempt must never leave an old canonical answer consumable if
        # this attempt later has missing, stale, or conflicting runtime facts.
        try:
            Path(paths.output_path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorkflowError("RUNTIME_EVIDENCE_INVALID", "cannot invalidate prior canonical output") from exc
        runtime_before_artifacts = runtime_artifact_snapshot(
            {"task.json": runtime_task_dir / "task.json"}
        )
    before_run = capture_repo(repo)
    before_changes = working_tree_paths(repo)
    attempt_id = accounting_context.attempt_id
    attempt_output = Path(paths.output_path).parent / "attempts" / f"{attempt_id}.json"
    attempt_events = Path(paths.logs_dir) / f"{attempt_id}.jsonl"
    attempt_started_ns = time.time_ns()
    attempt_output.parent.mkdir(parents=True, exist_ok=True)
    if attempt_output.exists():
        _fail("ATTEMPT_OUTPUT_COLLISION", "role attempt output path already exists")
    result: dict | None = None
    completed: subprocess.CompletedProcess | None = None
    attempt_error: BaseException | None = None
    attempt_recorded = False

    def _append_attempt(quality_outcome: str, runtime_usage: object) -> None:
        nonlocal attempt_recorded
        if paths.state_root is None or attempt_recorded:
            return
        _controller_cost_attempt(
            task["task_id"],
            task,
            role,
            CODEX_EXEC_ROLE_CONTRACT,
            (time.time_ns() - attempt_started_ns) / 1_000_000_000,
            len(prompt.encode("utf-8")),
            runtime_usage,
            accounting_context.retry_kind,
            quality_outcome,
            paths.state_root,
            attempt_id=attempt_id,
            compact_applied=(
                accepted_prompt_result is not None
                and accepted_prompt_result.mode == "compact"
                and accepted_prompt_result._gate_token is _COMPACT_GATE_TOKEN
            ),
        )
        attempt_recorded = True

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
    except BaseException as exc:
        attempt_error = exc
        raise
    finally:
        try:
            try:
                after_run = capture_repo(repo)
                after_changes = working_tree_paths(repo)
                if before_run.head != after_run.head:
                    _fail("HEAD_DRIFT", "repository HEAD changed during the role run")
                if role in READ_ONLY_ROLES and before_run != after_run:
                    _fail("READ_ONLY_ROLE_MODIFIED_REPO", f"read-only role {role} changed the repository")
                if task["task_type"] == "ACCEPTANCE":
                    assert_acceptance_candidate(task, repo)
                if role in TERRA_WRITE_ROLES:
                    actual_changes = after_changes - before_changes
                    assert_allowed_changes(
                        actual_changes,
                        (
                            luna_construction_step.write_scope
                            if luna_construction_step is not None
                            else task["allowed_write_paths"]
                        ),
                    )
                else:
                    actual_changes = after_changes - before_changes
            except BaseException as exc:
                attempt_error = exc
                raise
        finally:
            if attempt_error is not None:
                _append_attempt("FAILED", None)
    try:
        if result is None:
            _fail("INVALID_ROLE_RESULT", "role did not return a result")
        if paths.runtime_evidence_required:
            if runtime_store is None or runtime_task_dir is None or runtime_before_artifacts is None:
                _fail("RUNTIME_EVIDENCE_MISSING", "runtime state was not initialized")
            if runtime_sessions_dir is None:
                _fail("RUNTIME_EVIDENCE_MISSING", "runtime sessions directory was not initialized")
            events = parse_codex_jsonl(completed.stdout)
            thread_id = extract_codex_thread_id(events)
            role_config = _load_role_config(role)
            model = role_config.get("model")
            effort = role_config.get("reasoning_effort")
            sandbox = role_config.get("sandbox")
            if not all(isinstance(item, str) and item for item in (model, effort, sandbox)):
                _fail("RUNTIME_EVIDENCE_MISSING", "role configuration is incomplete")
            expected_runtime = codex_exec_contract(
                attempt_id=attempt_id,
                requested_role=role,
                model=model,
                reasoning_effort=effort,
                sandbox_policy=sandbox,
                cwd=str(repo),
            )
            controller_observation = codex_exec_observation(
                before_repository_snapshot=runtime_repository_snapshot(before_run),
                after_repository_snapshot=runtime_repository_snapshot(after_run),
                before_artifact_snapshot=runtime_before_artifacts,
                after_artifact_snapshot=runtime_artifact_snapshot(
                    {"task.json": runtime_task_dir / "task.json"}
                ),
                controller_prompt_forbids_writes=(
                    "Do not write, modify, delete, stage, commit, merge, or push repository files."
                    in prompt
                ),
            )
            rollout_observation = inspect_agent_runtime(
                runtime_sessions_dir,
                thread_id,
                CODEX_EXEC_ROLE_CONTRACT,
                Path(__file__).resolve().parents[1]
                / "plugins"
                / "ai-workflow"
                / "scripts"
                / "inspect-agent-runtime.sh",
            )
            observed_runtime = merge_runtime_observations(
                controller_observation, rollout_observation
            )
            runtime_evidence = verify_runtime_identity(expected_runtime, observed_runtime)
            write_runtime_evidence(runtime_store, task["task_id"], runtime_evidence)
            runtime_store.append_event(
                task["task_id"],
                {
                    "event_type": "RUNTIME_EVIDENCE_RECORDED",
                    "attempt_id": attempt_id,
                    "requested_role": role,
                    "thread_id": thread_id,
                    "execution_surface": CODEX_EXEC_ROLE_CONTRACT,
                    "runtime_evidence_sha256": hashlib.sha256(
                        _canonical_json(runtime_evidence.to_dict()).encode("utf-8")
                    ).hexdigest(),
                    "usage": extract_codex_usage(events),
                    "result_sha256": hashlib.sha256(
                        _canonical_json(result).encode("utf-8")
                    ).hexdigest(),
                },
            )
        validate_role_result(role, result, actual_changes)
        validate_verification_package(
            role,
            task,
            result,
            construction_step=luna_construction_step,
        )
    except BaseException:
        _append_attempt("FAILED", None)
        raise
    runtime_usage = (
        extract_codex_usage(parse_codex_jsonl(completed.stdout))
        if paths.runtime_evidence_required
        else None
    )
    try:
        atomic_write_json(paths.output_path, result)
    except BaseException:
        _append_attempt("FAILED", None)
        raise
    _append_attempt(str(result.get("status", "UNKNOWN")), runtime_usage)
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
    missing = sorted(REQUIRED_TASK_FIELDS - fields)
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")

    if task["schema_version"] != "ai-task-1":
        _fail("SCHEMA_VERSION", "schema_version must be ai-task-1")
    for field in ("task_id", "objective", "repository_root"):
        _require_nonempty_string(task, field)
    if "paired_case_id" in task:
        _require_nonempty_string(task, "paired_case_id")
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
        {"PLAN_READY", "REVIEW_READY", "BLOCKED", "ESCALATION_PROPOSED", "ABORTED"}
    ),
    "PLAN_READY": frozenset({"AWAITING_OWNER_DECISION", "ABORTED"}),
    "REVIEW_READY": frozenset({"AWAITING_OWNER_DECISION", "ABORTED"}),
    "ESCALATION_PROPOSED": frozenset({"AWAITING_OWNER_DECISION", "ABORTED"}),
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
    "IMPLEMENTATION_RUNNING": frozenset(
        {"IMPLEMENTED_CANDIDATE", "BLOCKED", "NEEDS_REPLAN", "ABORTED"}
    ),
    "IMPLEMENTED_CANDIDATE": frozenset(
        {"PRECHECK_RUNNING", "BLOCKED", "ABORTED"}
    ),
    "PRECHECK_RUNNING": frozenset({"PRECHECK_READY", "BLOCKED", "ABORTED"}),
    "PRECHECK_READY": frozenset(
        {"PLAN_OR_REVIEW_RUNNING", "BLOCKED", "ABORTED"}
    ),
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


def write_json_once(path: Path, value: object, *, conflict_code: str) -> str:
    """Atomically publish one frozen JSON artifact without replacing an existing one."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    temporary: Path | None = None
    parent_descriptor = -1
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        parent_descriptor = os.open(
            target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            _fail(conflict_code, f"{target.name} is already frozen")
        os.replace(temporary, target)
        temporary = None
        published = True
        os.fsync(parent_descriptor)
        return digest
    except WorkflowError:
        raise
    except OSError as exc:
        code = (
            "ATOMIC_WRITE_PUBLISHED_UNSYNCED"
            if published
            else "ATOMIC_WRITE_FAILED"
        )
        raise WorkflowError(code, f"cannot write {target.name}") from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
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

    def record_dispatch(
        self, task_id: str, dispatch_identity: str, payload: Mapping[str, object]
    ) -> Path:
        """Append one dispatch launch record, rejecting an identity replay.

        The lock covers both the duplicate scan and the append, so recovery or
        concurrent callers cannot race into launching the same canonical
        dispatch identity twice.  The ledger is append-only by construction.
        """

        with self.lock(task_id):
            return self._record_dispatch_locked(task_id, dispatch_identity, payload)

    def _record_dispatch_locked(
        self, task_id: str, dispatch_identity: str, payload: Mapping[str, object]
    ) -> Path:
        """Append a dispatch while the caller already holds this task's lock."""

        if (
            not isinstance(dispatch_identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", dispatch_identity)
        ):
            _fail("DISPATCH_IDENTITY_DRIFT", "dispatch identity must be a SHA256 digest")
        if not isinstance(payload, Mapping):
            _fail("INVALID_RECORD", "dispatch payload must be an object")
        record = dict(payload)
        if "dispatch_id" in record:
            _fail("INVALID_RECORD", "dispatch payload must not override dispatch_id")
        task_dir = self._require_task(task_id)
        ledger = task_dir / "dispatches.jsonl"
        try:
            lines = ledger.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            raise WorkflowError("DISPATCH_READ_ERROR", "cannot read dispatch ledger") from exc
        for line in lines:
            try:
                prior = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkflowError(
                    "DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains invalid JSON"
                ) from exc
            if (
                not isinstance(prior, dict)
                or not isinstance(prior.get("dispatch_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", prior["dispatch_id"])
            ):
                _fail("DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains an invalid record")
            if prior["dispatch_id"] == dispatch_identity:
                _fail("DUPLICATE_DISPATCH", "dispatch identity has already been recorded")
        record["dispatch_id"] = dispatch_identity
        append_jsonl(ledger, record)
        return ledger

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


try:
    from .ai_workflow_routing import (
        OptimizationAdviceResult,
        OptimizationPolicy,
        RuntimeRouteDecision,
        apply_route_advice as _apply_route_advice,
        decide_route as _decide_route,
        evaluate_and_apply_route_advice as _evaluate_and_apply_route_advice,
        persist_or_reuse_route_decision as _persist_or_reuse_route_decision,
        record_route_advice as _record_route_advice,
        record_route_decision as _record_route_decision,
        resolve_optimization_policy as _resolve_optimization_policy,
        resolve_role_policy as _resolve_role_policy,
        terra_os_read_only_role as _terra_os_read_only_role,
    )
except ImportError:  # direct script execution
    from ai_workflow_routing import (
        OptimizationAdviceResult,
        OptimizationPolicy,
        RuntimeRouteDecision,
        apply_route_advice as _apply_route_advice,
        decide_route as _decide_route,
        evaluate_and_apply_route_advice as _evaluate_and_apply_route_advice,
        persist_or_reuse_route_decision as _persist_or_reuse_route_decision,
        record_route_advice as _record_route_advice,
        record_route_decision as _record_route_decision,
        resolve_optimization_policy as _resolve_optimization_policy,
        resolve_role_policy as _resolve_role_policy,
        terra_os_read_only_role as _terra_os_read_only_role,
    )


def legacy_roles(task: Mapping[str, object]) -> tuple[str, ...]:
    """Expose the unchanged legacy route chain to the routing policy layer."""

    validate_task(task)
    return route(task)


def _configured_routing_mode(config: Mapping[str, object]) -> str:
    routing = config.get("routing")
    if not isinstance(routing, Mapping):
        _fail("ROUTE_INPUT_INVALID", "routing configuration is required")
    mode = routing.get("mode")
    if mode not in {"legacy", "shadow", "enforced"}:
        _fail("ROUTE_INPUT_INVALID", "unknown configured routing mode")
    return mode


def decide_route(
    task: Mapping[str, object],
    request: object,
    mode: str | None = None,
    *,
    construction_plan: object | None = None,
    construction_step_id: object = None,
) -> RuntimeRouteDecision:
    """Make a validated local route decision without executing a model."""

    validate_task(task)
    config = _load_workflow_config()
    configured_mode = _configured_routing_mode(config)
    return _decide_route(
        task,
        request,
        configured_mode if mode is None else mode,
        legacy_router=route,
        role_policy=_resolve_role_policy(config),
        construction_plan=construction_plan,
        construction_step_id=construction_step_id,
    )


def record_route_decision(
    store: WorkflowStore, task_id: str, decision: RuntimeRouteDecision
) -> Path:
    """Persist a strict route artifact and its append-only decision event."""

    return _record_route_decision(store, task_id, decision)


def persist_or_reuse_route_decision(
    store: WorkflowStore, task_id: str, decision: RuntimeRouteDecision
) -> RuntimeRouteDecision:
    """Write a route decision, or reuse the stored wire when retry semantics match."""

    return _persist_or_reuse_route_decision(store, task_id, decision)


def resolve_optimization_policy(config: object) -> OptimizationPolicy:
    """Read the closed optimization policy without writing configuration."""

    return _resolve_optimization_policy(config)


def evaluate_and_apply_route_advice(
    decision: RuntimeRouteDecision,
    *,
    recommended_route: object = None,
    state_root: Path | None = None,
    task: object = None,
    request: object = None,
    construction_plan: object | None = None,
    construction_step_id: object = None,
) -> OptimizationAdviceResult:
    """Compute the optimization gate from verified config and metrics."""

    return _evaluate_and_apply_route_advice(
        decision,
        recommended_route=recommended_route,
        state_root=state_root,
        task=task,
        request=request,
        construction_plan=construction_plan,
        construction_step_id=construction_step_id,
    )


def apply_route_advice(
    decision: RuntimeRouteDecision,
    *,
    recommended_route: object = None,
    state_root: Path | None = None,
    task: object = None,
    request: object = None,
    construction_plan: object | None = None,
    construction_step_id: object = None,
) -> OptimizationAdviceResult:
    """Public wrapper with no caller-supplied gate_result shortcut."""

    return _apply_route_advice(
        decision,
        recommended_route=recommended_route,
        state_root=state_root,
        task=task,
        request=request,
        construction_plan=construction_plan,
        construction_step_id=construction_step_id,
    )


def record_route_advice(
    store: WorkflowStore,
    task_id: str,
    advice: object,
    *,
    request_sha256: str | None = None,
) -> Path:
    """Persist a write-once route-advice sidecar bound to the stored decision."""

    return _record_route_advice(
        store, task_id, advice, request_sha256=request_sha256
    )


try:
    from .ai_workflow_planning import (
        ConstructionCheck,
        FrozenPlan,
        FrozenSubtask,
        construction_evidence_argv,
        dispatch_id,
        normalize_scope,
        ready_batch,
        record_dispatch,
        require_luna_construction_step,
        scope_owner_map,
        scopes_overlap,
        validate_plan,
    )
except ImportError:  # direct script execution
    from ai_workflow_planning import (
        ConstructionCheck,
        FrozenPlan,
        FrozenSubtask,
        construction_evidence_argv,
        dispatch_id,
        normalize_scope,
        ready_batch,
        record_dispatch,
        require_luna_construction_step,
        scope_owner_map,
        scopes_overlap,
        validate_plan,
    )


@dataclass(frozen=True)
class ConstructionExecutionContext:
    """One hash-bound construction launch derived solely from frozen artifacts."""

    plan: FrozenPlan
    step: FrozenSubtask
    dispatch_id: str
    task_sha256: str
    request_sha256: str
    role: str

    def contract(self) -> dict[str, object]:
        """Return the complete immutable role contract, with no route-wire policy."""

        return {
            "schema_version": "construction-contract-1",
            "dispatch_id": self.dispatch_id,
            "plan_sha256": self.plan.plan_sha256,
            "task_sha256": self.task_sha256,
            "request_sha256": self.request_sha256,
            "subtask_id": self.step.id,
            "role": self.role,
            "read_scope": list(self.step.read_scope),
            "write_scope": list(self.step.write_scope),
            "do_not_touch": list(self.step.do_not_touch),
            "verification_commands": list(self.step.verification_commands),
            "first_artifact": self.step.first_artifact,
            "construction_envelope": (
                self.step.construction_envelope.to_dict()
                if self.step.construction_envelope is not None
                else None
            ),
        }


def build_construction_role_prompt_result(
    task: Mapping[str, object],
    context: ConstructionExecutionContext,
    *,
    state_root: Path | None = None,
) -> PromptBuildResult:
    """Create the only prompt allowed for a bounded construction launch."""

    if not isinstance(context, ConstructionExecutionContext):
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction execution requires a frozen context")
    if context.role not in {"luna_construction", "terra_xhigh"}:
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction context has an invalid owner role")
    if context.role == "luna_construction" and context.step.construction_envelope is None:
        _fail("LUNA_ENVELOPE_INVALID", "luna construction context lacks its envelope")
    return build_role_prompt_result(
        context.role,
        task,
        context.contract(),
        (),
        state_root=state_root,
    )


def build_construction_role_prompt(
    task: Mapping[str, object],
    context: ConstructionExecutionContext,
    *,
    state_root: Path | None = None,
) -> str:
    """Create the only prompt allowed for a bounded construction launch."""

    return build_construction_role_prompt_result(
        task,
        context,
        state_root=state_root,
    ).prompt


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


def _metric_nonneg_int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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


def _normalize_metric_run(
    run: Mapping[str, object], *, controller_owned: bool = False
) -> dict[str, object]:
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
    period_declared = period in {"calibration", "experiment"}
    if not period_declared:
        period = "experiment"
    workflow_state = run.get("workflow_state")
    activity = run.get("activity")
    status = run.get("status")
    data_origin = run.get("data_origin")
    origin_declared = data_origin in {"runtime", "synthetic_fixture"}
    if not origin_declared:
        data_origin = "runtime"
    normalized = {
        "role": role,
        "timestamp_utc": _utc_timestamp(),
        "data_origin": data_origin,
        "period_declared": period_declared,
        "origin_declared": origin_declared,
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
    if run.get("compact_applied") is True:
        normalized["compact_applied"] = True
    if "p0_miss_count" in run:
        normalized["p0_miss_count"] = _metric_nonneg_int_or_none(run.get("p0_miss_count"))
    if "p1_miss_count" in run:
        normalized["p1_miss_count"] = _metric_nonneg_int_or_none(run.get("p1_miss_count"))
    # Preserve explicitly supplied cost-attempt fields as an append-only
    # nested record.  Legacy metric runs that have no paired-case identity do
    # not acquire synthetic token or price values.
    cost_fields = (
        "route",
        "execution_surface",
        "duration_seconds",
        "prompt_bytes",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "retry_kind",
        "verification_seconds",
        "quality_outcome",
        "paired_case_id",
        "evidence_class",
        "rate_snapshot_id",
        "projected_cost",
        "projected_cost_usd",
        "baseline_cost",
        "baseline_cost_usd",
        "new_cost",
        "new_cost_usd",
        "net_measured_cost_delta",
        "quality_delta_points",
    )
    nested_cost_record = run.get("cost_evidence") if controller_owned else None
    cost_record = (
        dict(nested_cost_record)
        if isinstance(nested_cost_record, Mapping)
        else {
            field: run[field]
            for field in cost_fields
            if controller_owned and field in run
        }
    )
    if "status" in run and "quality_outcome" not in cost_record:
        cost_record["quality_outcome"] = run["status"]
    if cost_record and (
        isinstance(nested_cost_record, Mapping)
        or any(
            field in cost_record
            for field in (
                "paired_case_id",
                "route",
                "execution_surface",
                "prompt_bytes",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "evidence_class",
                "rate_snapshot_id",
            )
        )
    ):
        cost_record.setdefault("role", role)
        normalized["cost_evidence"] = cost_record
    return normalized


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


def _record_metrics(
    task_id: str,
    run: Mapping[str, object],
    *,
    state_root: Path | None,
    controller_owned: bool,
) -> None:
    store = WorkflowStore(WORKFLOW_STATE_ROOT if state_root is None else state_root)
    path = store.metrics_path(task_id)
    document = _load_metrics_document(path, task_id)
    normalized_run = _normalize_metric_run(run, controller_owned=controller_owned)
    document["runs"].append(normalized_run)
    # The top-level value describes this newest raw attempt. It is null when
    # Codex JSONL did not explicitly provide a parseable usage number.
    document["token_usage"] = normalized_run["token_usage"]
    atomic_write_json(path, document)


def record_metrics(
    task_id: str,
    run: Mapping[str, object],
    *,
    state_root: Path | None = None,
) -> None:
    """Record untrusted observation metrics without accepting cost provenance."""

    _record_metrics(
        task_id,
        run,
        state_root=state_root,
        controller_owned=False,
    )


def _record_controller_metrics(
    task_id: str,
    run: Mapping[str, object],
    *,
    state_root: Path | None = None,
) -> None:
    """Controller-only cost-attempt persistence boundary."""

    _record_metrics(
        task_id,
        run,
        state_root=state_root,
        controller_owned=True,
    )


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
    prompt_bytes_total = 0
    prompt_record_count = 0
    compact_applied_count = 0
    input_tokens_total = 0
    cached_input_tokens_total = 0
    owner_gate_count = 0
    closed_task_ids: set[str] = set()
    stop_line_events: list[dict[str, object]] = []
    cost_records: list[Mapping[str, object]] = []
    cost_unavailable_attempts = 0
    synthetic_cost_attempts = 0
    p0_miss_total = 0
    p1_miss_total = 0
    p0_miss_missing = False
    p1_miss_missing = False
    gate_covered_runs = 0
    gate_periods: dict[str, set[str]] = {}
    gate_terra_first_status: dict[str, str | None] = {}
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
            cost_record = run.get("cost_evidence")
            if (
                run.get("data_origin") == "synthetic_fixture"
            ):
                synthetic_cost_attempts += 1
                continue
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
            if run.get("compact_applied") is True:
                compact_applied_count += 1
            gate_covered = (
                run.get("origin_declared") is True
                and run.get("data_origin") == "runtime"
                and run.get("period_declared") is True
            )
            if gate_covered:
                gate_covered_runs += 1
                period = run.get("period")
                if period in {"calibration", "experiment"}:
                    gate_periods.setdefault(task_id, set()).add(period)
                if role == "terra" and task_id not in gate_terra_first_status:
                    status = run.get("status")
                    gate_terra_first_status[task_id] = status if isinstance(status, str) else None
                if "p0_miss_count" not in run:
                    p0_miss_missing = True
                else:
                    miss = run.get("p0_miss_count")
                    if isinstance(miss, bool) or not isinstance(miss, int) or miss < 0:
                        p0_miss_missing = True
                    else:
                        p0_miss_total += miss
                if "p1_miss_count" not in run:
                    p1_miss_missing = True
                else:
                    miss = run.get("p1_miss_count")
                    if isinstance(miss, bool) or not isinstance(miss, int) or miss < 0:
                        p1_miss_missing = True
                    else:
                        p1_miss_total += miss
            if isinstance(cost_record, Mapping):
                prompt_bytes = cost_record.get("prompt_bytes")
                if isinstance(prompt_bytes, int) and not isinstance(prompt_bytes, bool):
                    prompt_bytes_total += prompt_bytes
                    prompt_record_count += 1
                input_tokens = cost_record.get("input_tokens")
                cached_input_tokens = cost_record.get("cached_input_tokens")
                if (
                    isinstance(input_tokens, int)
                    and not isinstance(input_tokens, bool)
                    and isinstance(cached_input_tokens, int)
                    and not isinstance(cached_input_tokens, bool)
                ):
                    input_tokens_total += input_tokens
                    cached_input_tokens_total += cached_input_tokens
                paired_case_id = cost_record.get("paired_case_id")
                if isinstance(paired_case_id, str) and paired_case_id.strip():
                    if gate_covered:
                        cost_records.append(dict(cost_record))
                elif run.get("cost_evidence") is not None:
                    # Controller evidence without a pre-registered pair is
                    # retained as unavailable rather than assigned a pair.
                    cost_unavailable_attempts += 1
        for event in _read_task_events(task_dir, task_id):
            if event.get("event_type") in {
                "OWNER_GATE_REACHED",
                "CONSTRUCTION_OWNER_GATE_REACHED",
            }:
                owner_gate_count += 1
            if event.get("new_state") == "CLOSED":
                closed_task_ids.add(task_id)
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

    def _cohort_for(task_id: str) -> str | None:
        task_periods = gate_periods.get(task_id, set())
        if "experiment" in task_periods:
            return "experiment"
        if "calibration" in task_periods:
            return "calibration"
        return None

    def _first_delivery_rate(cohort: str) -> float | None:
        statuses = [
            status
            for task_id, status in gate_terra_first_status.items()
            if _cohort_for(task_id) == cohort
        ]
        if not statuses:
            return None
        return sum(status == "IMPLEMENTED_CANDIDATE" for status in statuses) / len(statuses)
    cost_summary = aggregate_paired_cases(cost_records) if cost_records else {}
    model_call_count = sum(role_calls.values())
    closed_task_count = len(closed_task_ids)
    return {
        "calibration_task_count": calibration_tasks,
        "experiment_task_count": experiment_tasks,
        "role_calls": dict(sorted(role_calls.items())),
        "model_call_count": model_call_count,
        "closed_task_count": closed_task_count,
        "model_calls_per_closed_task": (
            model_call_count / closed_task_count if closed_task_count else None
        ),
        "owner_gate_count": owner_gate_count,
        "average_prompt_bytes": (
            prompt_bytes_total / prompt_record_count if prompt_record_count else None
        ),
        "compact_applied_count": compact_applied_count,
        "cache_hit_ratio": (
            cached_input_tokens_total / input_tokens_total
            if input_tokens_total
            else None
        ),
        "sol_participation_count": sum(
            count for role, count in role_calls.items() if role.startswith("sol_")
        ),
        "first_delivery_pass_rate": (
            first_delivery_passes / first_delivery_total if first_delivery_total else None
        ),
        "calibration_first_delivery_pass_rate": _first_delivery_rate("calibration"),
        "experiment_first_delivery_pass_rate": _first_delivery_rate("experiment"),
        "p0_miss_count": (
            None if gate_covered_runs == 0 or p0_miss_missing else p0_miss_total
        ),
        "p1_miss_count": (
            None if gate_covered_runs == 0 or p1_miss_missing else p1_miss_total
        ),
        "luna_unique_findings": len(luna_finding_ids),
        "luna_findings_adopted_by_sol": len(luna_finding_ids & adopted_luna_finding_ids),
        "luna_self_check_seconds": luna_self_check_seconds,
        "sol_verification_seconds": sol_verification_seconds,
        "semantic_reworks": semantic_reworks,
        "full_suite_runs": full_suite_runs,
        "end_to_end_seconds": end_to_end_seconds,
        "stop_line_events": stop_line_events,
        "cost_summary": cost_summary,
        "cost_unavailable_attempt_count": cost_unavailable_attempts,
        "synthetic_cost_attempt_count": synthetic_cost_attempts,
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
    calls_per_closed = metrics.get("model_calls_per_closed_task")
    calls_per_closed_text = (
        "n/a" if calls_per_closed is None else f"{float(calls_per_closed):.3f}"
    )
    average_prompt = metrics.get("average_prompt_bytes")
    average_prompt_text = (
        "n/a" if average_prompt is None else f"{float(average_prompt):.1f}"
    )
    cache_hit_ratio = metrics.get("cache_hit_ratio")
    cache_hit_text = (
        "n/a" if cache_hit_ratio is None else f"{float(cache_hit_ratio):.1%}"
    )
    lines.extend(
        (
            "",
            "## Efficiency",
            "",
            f"- Model calls: {metrics.get('model_call_count', 0)}",
            f"- Closed tasks: {metrics.get('closed_task_count', 0)}",
            f"- Model calls per closed task: {calls_per_closed_text}",
            f"- Owner gates reached: {metrics.get('owner_gate_count', 0)}",
            f"- Average prompt bytes: {average_prompt_text}",
            f"- Compact prompts applied: {metrics.get('compact_applied_count', 0)}",
            f"- Cached input ratio: {cache_hit_text}",
        )
    )
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
    cost_summary = metrics.get("cost_summary")
    if cost_summary is None:
        cost_records = metrics.get("cost_evidence")
        if isinstance(cost_records, list):
            cost_summary = aggregate_paired_cases(cost_records)
    cost_sections = render_cost_sections(
        cost_summary if isinstance(cost_summary, Mapping) else None,
        metrics.get("cost_claim_summary")
        if isinstance(metrics.get("cost_claim_summary"), Mapping)
        else None,
        int(metrics.get("cost_unavailable_attempt_count", 0) or 0),
    )
    synthetic_count = int(metrics.get("synthetic_cost_attempt_count", 0) or 0)
    if synthetic_count:
        lines.extend(
            (
                "",
                f"- synthetic fixture records: {synthetic_count} (not publishable)",
            )
        )
    lines.extend(("", cost_sections.rstrip("\n")))
    return _redact_log_text("\n".join(lines) + "\n")


FAKE_ROLE_RESULTS = {
    "luna": ("SUPPORTED", "EVIDENCE_READY"),
    "luna_construction": ("IMPLEMENTED_CANDIDATE", "PRECHECK_RUNNING"),
    "terra": ("IMPLEMENTED_CANDIDATE", "PRECHECK_RUNNING"),
    "terra_xhigh": ("IMPLEMENTED_CANDIDATE", "PRECHECK_RUNNING"),
    "terra_xhigh_planner": ("PLAN_READY", "AWAITING_OWNER_DECISION"),
    "terra_xhigh_reviewer": ("ACCEPTANCE_RECOMMENDED", "AWAITING_OWNER_DECISION"),
    "sol_planner": ("PLAN_READY", "AWAITING_OWNER_DECISION"),
    "sol_medium_supervisor": ("PLAN_READY", "AWAITING_OWNER_DECISION"),
    "sol_reviewer": ("ACCEPTANCE_RECOMMENDED", "AWAITING_OWNER_DECISION"),
    "sol_medium_reviewer": ("ACCEPTANCE_RECOMMENDED", "AWAITING_OWNER_DECISION"),
    "sol_xhigh": ("OPTION_A", "ESCALATION_PROPOSED"),
    "sol_xhigh_planner": ("OPTION_A", "ESCALATION_PROPOSED"),
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

    def run_construction(
        self,
        role: str,
        task: dict[str, object],
        context: ConstructionExecutionContext,
        *,
        attempt_context: AttemptAccountingContext | None = None,
    ) -> dict[str, object]:
        """Emit the one deterministic construction fixture bound to ``context``."""

        if not isinstance(context, ConstructionExecutionContext) or context.role != role:
            _fail("CONSTRUCTION_CONTEXT_INVALID", "fake construction run has no matching frozen context")
        result = self.run(role, task)
        result["changed_files"] = list(context.step.write_scope)
        if role != "luna_construction":
            return result
        envelope = context.step.construction_envelope
        if envelope is None:
            _fail("LUNA_ENVELOPE_INVALID", "fake Luna construction run lacks an envelope")
        checks = dict(envelope.evidence)
        result["claims"] = [
            {
                "id": "construction-claim",
                "kind": "FACT",
                "text": "The frozen construction contract has deterministic evidence.",
                "evidence_ids": ["L0", "L1", "L2"],
            }
        ]
        result["evidence"] = [
            {
                "id": level,
                "type": check.kind,
                "locator": check.artifact,
                "observation": _construction_evidence_observation(check),
            }
            for level, check in (("L0", checks["L0"]), ("L1", checks["L1"]), ("L2", checks["L2"]))
        ]
        negative = envelope.negative_checks[0]
        result["counter_checks"] = [
            {
                "target_claim_id": "construction-claim",
                "method": str(negative.command),
                "result": _construction_evidence_observation(negative),
            }
        ]
        return result


class TeamCallController(Protocol):
    """The bounded process/model boundary for a parsed Team Call."""

    def run_l0(self, argv: tuple[str, ...], cwd: Path) -> object:
        """Run one fixed L0 command in the already-validated repository."""

    def run_l1(self, task: Mapping[str, object], *, role: Literal["luna"]) -> Mapping[str, object]:
        """Run exactly the pinned Luna contract for one read-only L1 task."""


class TeamCallFakeController:
    """A deterministic Team Call controller that never starts a model."""

    is_live_model = False

    def run_l0(self, argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv not in set(L0_FIXED_ARGV.values()):
            _fail("TEAM_CALL_L0_INVALID", "L0 argv is not allowlisted")
        return subprocess.CompletedProcess(argv, 0, stdout="fake fixed L0 result\n", stderr="")

    def run_l1(
        self, execution: Mapping[str, object], *, role: Literal["luna"]
    ) -> Mapping[str, object]:
        if role != "luna":
            _fail("TEAM_CALL_ROLE_INVALID", "Team Call L1 is limited to luna")
        task = _validate_team_call_l1_execution(execution, "team-call-fake")
        evidence_digest = execution.consumed_evidence_sha256  # type: ignore[attr-defined]
        result = FakeRunner().run(role, task)
        result["evidence"][0]["observation"] = f"Pinned evidence SHA-256: {evidence_digest}."
        return result


@dataclass(frozen=True, slots=True)
class TeamCallProductionController:
    """Production Team Call boundary for fixed L0 and explicitly authorized Luna L1."""

    state_root: Path
    allow_live_model: bool = False
    runtime_sessions_dir: Path | None = None
    is_live_model = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root))
        if self.runtime_sessions_dir is not None:
            object.__setattr__(self, "runtime_sessions_dir", Path(self.runtime_sessions_dir))

    def run_l0(self, argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv not in set(L0_FIXED_ARGV.values()):
            _fail("TEAM_CALL_L0_INVALID", "L0 argv is not allowlisted")
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            cwd=Path(cwd),
        )

    def run_l1(
        self, execution: Mapping[str, object], *, role: Literal["luna"]
    ) -> Mapping[str, object]:
        if role != "luna":
            _fail("TEAM_CALL_ROLE_INVALID", "Team Call L1 is limited to luna")
        if not self.allow_live_model:
            _fail("LIVE_MODEL_NOT_AUTHORIZED", "--allow-live-model is required for the live runner")
        task_document = _validate_team_call_l1_execution(
            execution, CODEX_EXEC_ROLE_CONTRACT
        )
        task_dir = WorkflowStore(self.state_root)._require_task(str(task_document["task_id"]))
        _require_exact_team_call_stored_task(task_dir / "task.json", execution)  # type: ignore[arg-type]
        evidence_snapshot = _write_team_call_evidence_snapshot(
            task_dir, execution  # type: ignore[arg-type]
        )
        repository = Path(task_document["repository_root"])
        paths = RunPaths(
            repo=repository,
            output_path=task_dir / "luna-result.json",
            schema_path=ROLE_CONFIG_PATH.parent / "ai_workflow_result.schema.json",
            logs_dir=task_dir / "logs",
            state_root=self.state_root,
            runtime_evidence_required=True,
            runtime_sessions_dir=self.runtime_sessions_dir,
        )
        contract = {
            "acceptance_commands": task_document["acceptance_commands"],
            "verification_level": task_document["verification_level"],
            "team_call_attestation": _team_call_l1_attestation_record(
                execution  # type: ignore[arg-type]
            ),
        }
        prompt_result = build_role_prompt_result(
            "luna",
            task_document,
            contract,
            (evidence_snapshot.path,),
            state_root=self.state_root,
        )
        result = run_codex(
            "luna",
            task_document,
            prompt_result.prompt,
            paths,
            prompt_result=prompt_result,
        )
        _verify_team_call_evidence_snapshot(evidence_snapshot, execution)  # type: ignore[arg-type]
        return result


def _team_call_error_as_workflow(error: TeamCallError) -> WorkflowError:
    """Preserve the pure Team Call code and message at the controller boundary."""

    return WorkflowError(error.code, error.message)


def _resolve_team_call_repository(repository_root: Path) -> Path:
    """Resolve one direct-call repository root and require its Git worktree."""

    try:
        repository = Path(repository_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkflowError("REPOSITORY_NOT_FOUND", "repository_root does not exist") from exc
    if not repository.is_dir():
        _fail("REPOSITORY_NOT_FOUND", "repository_root does not exist")
    try:
        inside_work_tree = git(repository, "rev-parse", "--is-inside-work-tree")
        top_level = Path(git(repository, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except WorkflowError as exc:
        if exc.code == "GIT_COMMAND_FAILED":
            raise WorkflowError(
                "REPOSITORY_NOT_GIT_WORKTREE", "repository_root must be a Git worktree"
            ) from exc
        raise
    except (OSError, RuntimeError) as exc:
        raise WorkflowError(
            "REPOSITORY_NOT_GIT_WORKTREE", "repository_root must be a Git worktree"
        ) from exc
    if inside_work_tree != "true" or top_level != repository:
        _fail("REPOSITORY_NOT_GIT_WORKTREE", "repository_root must be a Git worktree")
    return repository


def _default_team_call_state_root(repository_root: Path) -> Path:
    """Choose durable per-repository Team Call state outside the target worktree."""

    repository = Path(repository_root).resolve()
    repository_digest = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()
    candidates: list[Path] = []
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        xdg_path = Path(xdg_state_home).expanduser()
        if xdg_path.is_absolute():
            candidates.append(xdg_path)
    candidates.extend(
        (
            Path.home() / ".local" / "state",
            Path(tempfile.gettempdir()).resolve() / f"ai-workflow-state-{os.getuid()}",
        )
    )
    for base in candidates:
        candidate = (base / "ai-workflow" / "team-call" / repository_digest).resolve()
        if not candidate.is_relative_to(repository):
            return candidate
    return repository.parent / f".ai-workflow-team-call-state-{repository_digest}"


@dataclass(frozen=True)
class _TeamCallFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _TeamCallEvidencePin:
    evidence_path: str
    components: tuple[tuple[str, _TeamCallFileIdentity], ...]
    evidence_bytes: bytes


@dataclass(frozen=True)
class _TeamCallL1Execution(Mapping[str, object]):
    """Immutable task/evidence attestation consumed by the closed L1 route."""

    task_json: str
    stored_task_bytes: bytes
    task_sha256: str
    role: Literal["luna"]
    execution_surface: str
    evidence_path: str
    consumed_evidence_sha256: str
    pinned_evidence_bytes: bytes

    def task_document(self) -> dict[str, object]:
        document = json.loads(self.task_json)
        if not isinstance(document, dict):
            _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call task attestation is invalid")
        return document

    def __getitem__(self, key: str) -> object:
        return self.task_document()[key]

    def __iter__(self):
        return iter(self.task_document())

    def __len__(self) -> int:
        return len(self.task_document())

    def __setitem__(self, key: str, value: object) -> None:
        if key == "allowed_write_paths":
            _fail("TEAM_CALL_WRITE_SCOPE_INVALID", "Team Call L1 allowed_write_paths must stay empty")
        _fail("TEAM_CALL_TASK_MUTATED", "Team Call L1 task attestation is immutable")


@dataclass(frozen=True)
class _TeamCallEvidenceSnapshot:
    path: Path
    identity: _TeamCallFileIdentity
    consumed_evidence_sha256: str


def _team_call_file_identity(value: os.stat_result) -> _TeamCallFileIdentity:
    return _TeamCallFileIdentity(
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _team_call_l1_evidence(repository: Path, evidence_path: str) -> _TeamCallEvidencePin:
    """Pin every evidence component with component-wise no-follow identities."""

    components = Path(evidence_path).parts
    if not components or any(part in {"", ".", ".."} for part in components):
        _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence path is invalid")
    root_fd: int | None = None
    current_fd: int | None = None
    identities: list[tuple[str, _TeamCallFileIdentity]] = []
    evidence_bytes = b""
    try:
        root_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current_fd = root_fd
        identities.append((".", _team_call_file_identity(os.fstat(current_fd))))
        for index, component in enumerate(components):
            before = os.lstat(component, dir_fd=current_fd)
            if stat.S_ISLNK(before.st_mode):
                _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence must not contain a symlink")
            final_component = index == len(components) - 1
            if final_component:
                if not stat.S_ISREG(before.st_mode):
                    _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence must be a regular file")
                flags = os.O_RDONLY | os.O_NOFOLLOW
            else:
                if not stat.S_ISDIR(before.st_mode):
                    _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence parent must be a directory")
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            next_fd = os.open(component, flags, dir_fd=current_fd)
            after = os.fstat(next_fd)
            if _team_call_file_identity(before) != _team_call_file_identity(after):
                os.close(next_fd)
                _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence changed while being validated")
            if final_component:
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(next_fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                if _team_call_file_identity(os.fstat(next_fd)) != _team_call_file_identity(after):
                    os.close(next_fd)
                    _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence changed while being read")
                evidence_bytes = b"".join(chunks)
            identities.append((component, _team_call_file_identity(after)))
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return _TeamCallEvidencePin(evidence_path, tuple(identities), evidence_bytes)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowError(
            "TEAM_CALL_EVIDENCE_UNSAFE",
            "Team Call evidence must be a regular file beneath repository_root",
        ) from exc
    finally:
        if current_fd is not None and current_fd != root_fd:
            os.close(current_fd)
        if root_fd is not None:
            os.close(root_fd)


def _revalidate_team_call_l1_evidence(
    repository: Path, evidence_pin: _TeamCallEvidencePin
) -> _TeamCallEvidencePin:
    """Require the same no-follow component identities immediately before L1 launch."""

    current = _team_call_l1_evidence(repository, evidence_pin.evidence_path)
    if (
        current.components != evidence_pin.components
        or current.evidence_bytes != evidence_pin.evidence_bytes
    ):
        _fail("TEAM_CALL_EVIDENCE_UNSAFE", "Team Call evidence changed before controller invocation")
    return current


def _write_team_call_evidence_snapshot(
    task_dir: Path, execution: _TeamCallL1Execution
) -> _TeamCallEvidenceSnapshot:
    """Create the content-addressed, write-once evidence handoff for Luna."""

    _validate_team_call_l1_execution(execution, CODEX_EXEC_ROLE_CONTRACT)
    snapshot_dir = Path(task_dir) / "team-call-evidence"
    try:
        snapshot_dir.mkdir(mode=0o700)
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            _fail("TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID", "evidence snapshot directory is unsafe")
        snapshot_path = snapshot_dir / f"{execution.consumed_evidence_sha256}.snapshot"
        descriptor = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
    except FileExistsError:
        _fail("TEAM_CALL_EVIDENCE_SNAPSHOT_EXISTS", "evidence snapshot is already frozen")
    except OSError as exc:
        raise WorkflowError(
            "TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID",
            "cannot create the Team Call evidence snapshot",
        ) from exc
    try:
        remaining = memoryview(execution.pinned_evidence_bytes)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                _fail("TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID", "cannot write evidence snapshot")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        identity = _team_call_file_identity(os.fstat(descriptor))
    except OSError as exc:
        raise WorkflowError(
            "TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID",
            "cannot freeze the Team Call evidence snapshot",
        ) from exc
    finally:
        os.close(descriptor)
    snapshot = _TeamCallEvidenceSnapshot(
        snapshot_path,
        identity,
        execution.consumed_evidence_sha256,
    )
    _verify_team_call_evidence_snapshot(snapshot, execution)
    return snapshot


def _verify_team_call_evidence_snapshot(
    snapshot: _TeamCallEvidenceSnapshot, execution: _TeamCallL1Execution
) -> None:
    """Reopen the snapshot without following links and require the bound bytes."""

    if snapshot.consumed_evidence_sha256 != execution.consumed_evidence_sha256:
        _fail("TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID", "evidence snapshot digest drifted")
    descriptor: int | None = None
    try:
        descriptor = os.open(snapshot.path, os.O_RDONLY | os.O_NOFOLLOW)
        before = _team_call_file_identity(os.fstat(descriptor))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = _team_call_file_identity(os.fstat(descriptor))
    except OSError as exc:
        raise WorkflowError(
            "TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID",
            "cannot verify the Team Call evidence snapshot",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    observed = b"".join(chunks)
    if (
        before != snapshot.identity
        or after != snapshot.identity
        or observed != execution.pinned_evidence_bytes
        or hashlib.sha256(observed).hexdigest() != execution.consumed_evidence_sha256
    ):
        _fail("TEAM_CALL_EVIDENCE_SNAPSHOT_INVALID", "evidence snapshot identity drifted")


def _team_call_regular_digest(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        block = os.read(file_descriptor, 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _team_call_filesystem_snapshot(
    repository: Path, *, exclude_git_directory: bool = True
) -> tuple[tuple[object, ...], ...]:
    """Snapshot a tree with no-follow metadata and regular-file content."""

    root_fd: int | None = None
    entries: list[tuple[object, ...]] = []

    def walk(directory_fd: int, relative: str) -> None:
        directory_identity = _team_call_file_identity(os.fstat(directory_fd))
        entries.append((relative, directory_identity, None))
        for name in sorted(os.listdir(directory_fd)):
            item_relative = name if relative == "." else f"{relative}/{name}"
            before = os.lstat(name, dir_fd=directory_fd)
            if (
                exclude_git_directory
                and relative == "."
                and name == ".git"
                and stat.S_ISDIR(before.st_mode)
            ):
                continue
            identity = _team_call_file_identity(before)
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                try:
                    if _team_call_file_identity(os.fstat(child_fd)) != identity:
                        _fail("READ_ONLY_FILESYSTEM_SNAPSHOT_INVALID", "filesystem changed during snapshot")
                    walk(child_fd, item_relative)
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(before.st_mode):
                child_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    if _team_call_file_identity(os.fstat(child_fd)) != identity:
                        _fail("READ_ONLY_FILESYSTEM_SNAPSHOT_INVALID", "filesystem changed during snapshot")
                    digest = _team_call_regular_digest(child_fd)
                    if _team_call_file_identity(os.fstat(child_fd)) != identity:
                        _fail("READ_ONLY_FILESYSTEM_SNAPSHOT_INVALID", "filesystem changed during snapshot")
                finally:
                    os.close(child_fd)
                entries.append((item_relative, identity, digest))
                continue
            entries.append((item_relative, identity, None))

    try:
        root_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        walk(root_fd, ".")
        return tuple(entries)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowError(
            "READ_ONLY_FILESYSTEM_SNAPSHOT_INVALID", "cannot safely snapshot repository files"
        ) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _team_call_git_control_snapshot(repository: Path) -> tuple[tuple[str, object], ...]:
    """Snapshot the per-worktree and common Git control directories."""

    control_paths: set[Path] = set()
    for argument in ("--absolute-git-dir", "--git-common-dir"):
        raw_path = Path(git(repository, "rev-parse", argument))
        control_path = raw_path if raw_path.is_absolute() else repository / raw_path
        try:
            control_paths.add(control_path.resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise WorkflowError(
                "READ_ONLY_FILESYSTEM_SNAPSHOT_INVALID",
                "cannot resolve Git control directory",
            ) from exc
    return tuple(
        (str(path), _team_call_filesystem_snapshot(path, exclude_git_directory=False))
        for path in sorted(control_paths, key=str)
    )


def _next_team_call_task_id(store: WorkflowStore) -> str:
    """Allocate the next daily task ID while the Team Call registry lock is held."""

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"AWF-{day}-"
    suffixes: list[int] = []
    try:
        task_directories = tuple(store.root.glob(f"{prefix}*")) if store.root.is_dir() else ()
    except OSError as exc:
        raise WorkflowError("TASK_ID_ALLOCATION_FAILED", "cannot scan Team Call task directories") from exc
    for task_directory in task_directories:
        if not task_directory.is_dir():
            continue
        suffix = task_directory.name.removeprefix(prefix)
        if suffix.isdigit() and len(suffix) >= 3:
            suffixes.append(int(suffix))
    return f"{prefix}{(max(suffixes, default=0) + 1):03d}"


def _team_call_l1_task(
    *, task_id: str, repository: Path, objective: str, evidence_path: str
) -> dict[str, object]:
    """Build the complete, read-only Luna task envelope for one file evidence request."""

    task = {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "PLAN",
        "objective": f"Team Call read-only evidence review: {objective}",
        "repository_root": str(repository),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": [evidence_path],
        "allowed_write_paths": [],
        "forbidden_actions": ["merge", "push", "change_constitution"],
        "risk_flags": [],
        "acceptance_commands": [],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL"],
    }
    validate_task(task)
    return task


def _team_call_l1_execution(
    task: Mapping[str, object],
    stored_task: Mapping[str, object],
    stored_task_path: Path,
    evidence_pin: _TeamCallEvidencePin,
    execution_surface: str,
) -> _TeamCallL1Execution:
    """Bind the exact stored task, Luna role, surface, and pinned evidence bytes."""

    task_json = _canonical_json(dict(task))
    if task_json != _canonical_json(dict(stored_task)):
        _fail("TASK_STORE_MISMATCH", "Team Call task input does not match the stored task")
    try:
        stored_task_bytes = Path(stored_task_path).read_bytes()
    except OSError as exc:
        raise WorkflowError("TASK_READ_ERROR", "cannot hash the stored Team Call task") from exc
    if stored_task_bytes != (task_json + "\n").encode("utf-8"):
        _fail("TASK_STORE_MISMATCH", "Team Call stored task bytes are not canonical")
    execution = _TeamCallL1Execution(
        task_json=task_json,
        stored_task_bytes=stored_task_bytes,
        task_sha256=hashlib.sha256(stored_task_bytes).hexdigest(),
        role="luna",
        execution_surface=execution_surface,
        evidence_path=evidence_pin.evidence_path,
        consumed_evidence_sha256=hashlib.sha256(evidence_pin.evidence_bytes).hexdigest(),
        pinned_evidence_bytes=evidence_pin.evidence_bytes,
    )
    _validate_team_call_l1_execution(execution, execution_surface)
    return execution


def _validate_team_call_l1_execution(
    execution: object, expected_surface: str
) -> dict[str, object]:
    """Validate the immutable L1 execution attestation at every boundary."""

    if type(execution) is not _TeamCallL1Execution:
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call L1 execution is not attested")
    if execution.role != "luna" or execution.execution_surface != expected_surface:
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call L1 role or surface is not attested")
    if execution.stored_task_bytes != (execution.task_json + "\n").encode("utf-8"):
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call stored task bytes are invalid")
    if hashlib.sha256(execution.stored_task_bytes).hexdigest() != execution.task_sha256:
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call stored task digest is invalid")
    if (
        hashlib.sha256(execution.pinned_evidence_bytes).hexdigest()
        != execution.consumed_evidence_sha256
    ):
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call evidence digest is invalid")
    task = execution.task_document()
    validate_task(task)
    if _canonical_json(task) != execution.task_json:
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call task encoding is not canonical")
    if task["allowed_write_paths"] != []:
        _fail("TEAM_CALL_WRITE_SCOPE_INVALID", "Team Call L1 allowed_write_paths must stay empty")
    if task["authoritative_files"] != [execution.evidence_path]:
        _fail("TEAM_CALL_ATTESTATION_INVALID", "Team Call evidence path is not task-bound")
    return task


def _team_call_l1_attestation_record(
    execution: _TeamCallL1Execution,
) -> dict[str, str]:
    """Return the immutable execution identity persisted with the result."""

    _validate_team_call_l1_execution(execution, execution.execution_surface)
    return {
        "task_sha256": execution.task_sha256,
        "role": execution.role,
        "execution_surface": execution.execution_surface,
        "evidence_path": execution.evidence_path,
        "consumed_evidence_sha256": execution.consumed_evidence_sha256,
    }


def _require_exact_team_call_stored_task(
    task_path: Path, execution: _TeamCallL1Execution
) -> dict[str, object]:
    """Require the current frozen task artifact to retain its attested bytes."""

    try:
        current_bytes = Path(task_path).read_bytes()
    except OSError as exc:
        raise WorkflowError("TASK_READ_ERROR", "cannot read the stored Team Call task") from exc
    if current_bytes != execution.stored_task_bytes:
        _fail("TASK_STORE_MISMATCH", "Team Call stored task bytes changed during L1")
    current = load_task(task_path)
    if current != execution.task_document():
        _fail("TASK_STORE_MISMATCH", "Team Call stored task identity changed during L1")
    return current


def _team_call_result_digest(
    state_root: Path, receipt: TeamCallReceipt, metadata: Mapping[str, object]
) -> str:
    """Persist receipt-addressed output metadata and return its canonical digest."""

    result = {
        "schema_version": "team-call-result-1",
        "call_id": receipt.call_id,
        **dict(metadata),
    }
    canonical = _canonical_json(result)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    write_json_once(
        Path(state_root) / "team-call-results" / f"{receipt.call_id}.json",
        result,
        conflict_code="TEAM_CALL_RESULT_EXISTS",
    )
    return digest


def _team_call_l0_metadata(
    completed: object, argv: tuple[str, ...], repository: Path
) -> dict[str, object]:
    """Accept only a completed fixed L0 process and retain its bounded output."""

    returncode = getattr(completed, "returncode", None)
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        _fail("TEAM_CALL_L0_INVALID_OUTPUT", "fixed L0 command returned no integer exit status")
    if returncode != 0:
        _fail("TEAM_CALL_L0_FAILED", "fixed L0 command failed")

    def output(name: str) -> str | None:
        value = getattr(completed, name, None)
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        _fail("TEAM_CALL_L0_INVALID_OUTPUT", f"fixed L0 {name} is invalid")
        raise AssertionError("unreachable")

    return {
        "kind": "L0",
        "argv": list(argv),
        "cwd": str(repository),
        "returncode": returncode,
        "stdout": output("stdout"),
        "stderr": output("stderr"),
    }


def _require_trusted_team_call_controller(controller: object) -> TeamCallController:
    """Admit exact controllers with no instance-shadowed execution methods."""

    if type(controller) not in {TeamCallFakeController, TeamCallProductionController}:
        _fail(
            "TEAM_CALL_CONTROLLER_UNTRUSTED",
            "Team Call controller must hold the trusted controller capability",
        )
    instance_attributes = getattr(controller, "__dict__", {})
    if isinstance(instance_attributes, dict) and {
        "run_l0",
        "run_l1",
    }.intersection(instance_attributes):
        _fail(
            "TEAM_CALL_CONTROLLER_UNTRUSTED",
            "Team Call controller execution methods must not be instance-shadowed",
        )
    controller_type = type(controller)
    if not callable(controller_type.run_l0) or not callable(controller_type.run_l1):
        _fail("TEAM_CALL_CONTROLLER_UNTRUSTED", "Team Call controller is incomplete")
    return controller  # type: ignore[return-value]


def _team_call_execution_surface(controller: TeamCallController) -> str:
    if type(controller) is TeamCallFakeController:
        return "team-call-fake"
    if type(controller) is TeamCallProductionController:
        return CODEX_EXEC_ROLE_CONTRACT
    _fail("TEAM_CALL_CONTROLLER_UNTRUSTED", "Team Call controller type is not closed")
    raise AssertionError("unreachable")


def _run_trusted_team_call_l0(
    controller: TeamCallController, argv: tuple[str, ...], repository: Path
) -> object:
    """Execute L0 through the exact class surface, never an instance-bound shadow."""

    if type(controller) is TeamCallFakeController:
        return TeamCallFakeController.run_l0(controller, argv, repository)
    if type(controller) is TeamCallProductionController:
        return TeamCallProductionController.run_l0(controller, argv, repository)
    _fail("TEAM_CALL_CONTROLLER_UNTRUSTED", "Team Call controller type is not closed")
    raise AssertionError("unreachable")


def _run_trusted_team_call_l1(
    controller: TeamCallController, execution: _TeamCallL1Execution
) -> Mapping[str, object]:
    """Execute Luna through the exact class surface and immutable attestation."""

    expected_surface = _team_call_execution_surface(controller)
    _validate_team_call_l1_execution(execution, expected_surface)
    if type(controller) is TeamCallFakeController:
        result = TeamCallFakeController.run_l1(controller, execution, role="luna")
    elif type(controller) is TeamCallProductionController:
        result = TeamCallProductionController.run_l1(controller, execution, role="luna")
    else:
        _fail("TEAM_CALL_CONTROLLER_UNTRUSTED", "Team Call controller type is not closed")
        raise AssertionError("unreachable")
    _validate_team_call_l1_execution(execution, expected_surface)
    return result


def run_team_call(
    message: str,
    *,
    repository_root: Path,
    state_root: Path,
    controller: TeamCallController,
) -> TeamCallReceipt:
    """Run one parsed Team Call exactly once through the append-only registry."""

    trusted_controller = _require_trusted_team_call_controller(controller)

    try:
        call = parse_team_call(message)
        if call is None:
            _fail("TEAM_CALL_INVALID", "message must start with a team call directive")
        intent = classify_team_call(call)
    except TeamCallError as exc:
        raise _team_call_error_as_workflow(exc) from exc

    repository: Path | None = None
    if intent.disposition in {"DIRECT_L0", "DIRECT_L1"}:
        repository = _resolve_team_call_repository(repository_root)
    registry = TeamCallRegistry(Path(state_root))

    def execute(receipt: TeamCallReceipt) -> TeamCallRoute:
        if intent.disposition == "PLAN_REQUIRED":
            return TeamCallRoute(task_id=None, result_sha256=None)
        if intent.disposition == "DIRECT_L0":
            if repository is None or intent.l0_action is None:
                _fail("TEAM_CALL_L0_INVALID", "parsed L0 action is incomplete")
            argv = L0_FIXED_ARGV.get(intent.l0_action)
            if argv is None:
                _fail("TEAM_CALL_L0_INVALID", "parsed L0 action is not allowlisted")
            completed = _run_trusted_team_call_l0(trusted_controller, argv, repository)
            digest = _team_call_result_digest(
                Path(state_root), receipt, _team_call_l0_metadata(completed, argv, repository)
            )
            return TeamCallRoute(task_id=None, result_sha256=digest)
        if intent.disposition == "DIRECT_L1":
            if repository is None or intent.evidence_path is None:
                _fail("TEAM_CALL_EVIDENCE_UNSAFE", "parsed L1 evidence is incomplete")
            evidence_pin = _team_call_l1_evidence(repository, intent.evidence_path)
            _reject_dirty_input(
                repository,
                "DIRTY_READ_ONLY_REPOSITORY",
                "Team Call L1 requires a clean repository",
            )
            store = WorkflowStore(Path(state_root))
            task = _team_call_l1_task(
                task_id=_next_team_call_task_id(store),
                repository=repository,
                objective=call.objective,
                evidence_path=intent.evidence_path,
            )
            stored_path = store.create_task(task)
            stored_task = load_task(stored_path)
            if stored_task != task:
                _fail("TASK_STORE_MISMATCH", "Team Call task was not persisted exactly")
            repository_before = capture_repo(repository)
            repository_files_before = _team_call_filesystem_snapshot(repository)
            git_control_before = _team_call_git_control_snapshot(repository)
            invocation_pin = _revalidate_team_call_l1_evidence(repository, evidence_pin)
            execution = _team_call_l1_execution(
                task,
                stored_task,
                stored_path,
                invocation_pin,
                _team_call_execution_surface(trusted_controller),
            )
            result = _run_trusted_team_call_l1(trusted_controller, execution)
            _require_exact_team_call_stored_task(stored_path, execution)
            repository_files_after = _team_call_filesystem_snapshot(repository)
            git_control_after = _team_call_git_control_snapshot(repository)
            if repository_files_after != repository_files_before:
                repository_after = capture_repo(repository)
                if repository_after != repository_before:
                    _fail(
                        "TEAM_CALL_REPOSITORY_CHANGED",
                        "Team Call L1 changed repository HEAD or status",
                    )
                _fail(
                    "READ_ONLY_FILESYSTEM_CHANGED",
                    "Team Call L1 changed repository filesystem state",
                )
            if git_control_after != git_control_before:
                _fail(
                    "READ_ONLY_FILESYSTEM_CHANGED",
                    "Team Call L1 changed repository filesystem state",
                )
            repository_after = capture_repo(repository)
            if repository_after != repository_before:
                _fail(
                    "TEAM_CALL_REPOSITORY_CHANGED",
                    "Team Call L1 changed repository HEAD or status",
                )
            if isinstance(result, Mapping) and isinstance(result.get("changed_files"), list) and result["changed_files"]:
                _fail("READ_ONLY_ROLE_MODIFIED_REPO", "read-only role luna claimed repository changes")
            actual_changes = working_tree_paths(repository)
            validate_role_result("luna", result, actual_changes)
            validate_verification_package("luna", task, result)
            digest = _team_call_result_digest(
                Path(state_root),
                receipt,
                {
                    "kind": "L1",
                    "task_id": task["task_id"],
                    "role": "luna",
                    "attestation": _team_call_l1_attestation_record(execution),
                    "result": dict(result),
                },
            )
            return TeamCallRoute(task_id=str(task["task_id"]), result_sha256=digest)
        _fail("TEAM_CALL_INVALID", "parsed Team Call disposition is unsupported")
        raise AssertionError("unreachable")

    try:
        return registry.execute_once(call, intent, execute)
    except TeamCallError as exc:
        raise _team_call_error_as_workflow(exc) from exc


class CodexConstructionRunner:
    """Live construction runner whose prompt is derived only from a frozen context."""

    is_live_model = True
    owns_cost_attempt_accounting = True

    def __init__(self, state_root: Path, runtime_sessions_dir: Path | None):
        self.state_root = Path(state_root)
        self.runtime_sessions_dir = runtime_sessions_dir

    def run(self, role: str, task: dict[str, object], **_: object) -> Mapping[str, object]:
        _fail("CONSTRUCTION_CONTEXT_REQUIRED", "live construction cannot run a generic role prompt")
        raise AssertionError("unreachable")

    def run_construction(
        self,
        role: str,
        task: dict[str, object],
        context: ConstructionExecutionContext,
        *,
        attempt_context: AttemptAccountingContext | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(context, ConstructionExecutionContext) or context.role != role:
            _fail("CONSTRUCTION_CONTEXT_INVALID", "live construction context does not match role")
        task_dir = WorkflowStore(self.state_root)._require_task(str(task["task_id"]))
        repository = _execution_repo(task, role)
        paths = RunPaths(
            repo=repository,
            output_path=task_dir / f"{role}-result.json",
            schema_path=Path(task["repository_root"]) / "config" / "ai_workflow_result.schema.json",
            logs_dir=task_dir / "logs",
            state_root=self.state_root,
            runtime_evidence_required=True,
            runtime_sessions_dir=self.runtime_sessions_dir,
        )
        prompt_result = build_construction_role_prompt_result(
            task,
            context,
            state_root=self.state_root,
        )
        return run_codex(
            role,
            task,
            prompt_result.prompt,
            paths,
            attempt_context=attempt_context,
            construction_plan=context.plan.to_dict(),
            construction_step_id=context.step.id,
            construction_context=context,
            prompt_result=prompt_result,
        )


class Runner(Protocol):
    """The small, injectable boundary used by the gated orchestrator."""

    is_live_model: bool
    owns_cost_attempt_accounting: bool

    def run(
        self,
        role: str,
        task: dict[str, object],
        *,
        attempt_context: AttemptAccountingContext | None = None,
    ) -> Mapping[str, object]:
        """Return one ai-result-1-compatible role result."""

    def run_construction(
        self,
        role: str,
        task: dict[str, object],
        context: ConstructionExecutionContext,
        *,
        attempt_context: AttemptAccountingContext | None = None,
    ) -> Mapping[str, object]:
        """Run only a prompt generated from the provided frozen construction context."""


@dataclass(frozen=True)
class RetryLimits:
    """Configured closed-set limits for each retry class."""

    technical_retries: int
    implementation_reworks: int
    cross_model_escalations: int


def _retry_limits_from_config(config: object) -> RetryLimits:
    if not isinstance(config, Mapping):
        _fail("INVALID_POLICY", "workflow configuration must be an object")
    policy = config.get("policy")
    if not isinstance(policy, Mapping):
        _fail("INVALID_POLICY", "workflow policy configuration is required")
    values: list[int] = []
    for name in (
        "max_technical_retries",
        "max_implementation_reworks",
        "max_cross_model_escalations",
    ):
        value = policy.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _fail("INVALID_POLICY", f"policy.{name} must be a non-negative integer")
        values.append(value)
    return RetryLimits(*values)


def _configured_retry_limits() -> RetryLimits:
    return _retry_limits_from_config(_load_workflow_config())


@dataclass
class RetryBudget:
    """Retry counters bounded by the pinned workflow policy."""

    technical_retries: int = 0
    implementation_reworks: int = 0
    cross_model_escalations: int = 0
    limits: RetryLimits = field(default_factory=_configured_retry_limits)

    def _consume(self, field: str, detail: str) -> None:
        if getattr(self, field) >= getattr(self.limits, field):
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
    *,
    construction_context: ConstructionExecutionContext | None = None,
    state_root: Path | None = None,
) -> tuple[Mapping[str, object] | None, str]:
    """Run one role, allowing only the single persisted technical retry."""

    if role == "luna_construction" and construction_context is None:
        _fail(
            "LUNA_ENVELOPE_INVALID",
            "generic pipeline dispatch cannot launch luna construction without its envelope",
        )
    if construction_context is not None and (
        construction_context.role != role
        or role not in {"luna_construction", "terra_xhigh"}
    ):
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction role does not match its frozen context")

    retry_kind = "none"
    while True:
        runner_owns_attempt_accounting = bool(
            getattr(runner, "owns_cost_attempt_accounting", False)
        )
        attempt_context = (
            _new_attempt_accounting_context(task_id, role, retry_kind)
            if runner_owns_attempt_accounting
            else None
        )
        try:
            guarded_repo: Path | None = None
            before_snapshot: RepoSnapshot | None = None
            before_changes: set[str] | None = None
            if getattr(runner, "is_live_model", False):
                guarded_repo = _execution_repo(task, role)
                if role in TERRA_WRITE_ROLES:
                    _assert_terra_worktree_authorized(
                        task, guarded_repo, state_root or WORKFLOW_STATE_ROOT
                    )
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
            attempt_error: BaseException | None = None
            runtime_usage = None
            try:
                if construction_context is not None:
                    run_construction = getattr(runner, "run_construction", None)
                    if not callable(run_construction):
                        _fail(
                            "CONSTRUCTION_CONTEXT_INVALID",
                            "construction runner must accept the frozen contract",
                        )
                    if attempt_context is None:
                        result = run_construction(role, task, construction_context)
                    else:
                        result = run_construction(
                            role,
                            task,
                            construction_context,
                            attempt_context=attempt_context,
                        )
                elif attempt_context is None:
                    result = runner.run(role, task)
                else:
                    result = runner.run(role, task, attempt_context=attempt_context)
                if construction_context is not None:
                    result = _bind_controller_construction_evidence(
                        result, task, construction_context
                    )
                    if construction_context.role == "luna_construction":
                        store.append_event(
                            task_id,
                            {
                                "event_type": "CONSTRUCTION_EVIDENCE_RECORDED",
                                "timestamp_utc": _utc_timestamp(),
                                "dispatch_id": construction_context.dispatch_id,
                                "evidence": result.get("evidence"),
                                "counter_checks": result.get("counter_checks"),
                            },
                        )
            except BaseException as exc:
                attempt_error = exc
                raise
            finally:
                try:
                    try:
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
                            if role in TERRA_WRITE_ROLES:
                                assert_allowed_changes(
                                    actual_changes,
                                    (
                                        construction_context.step.write_scope
                                        if construction_context is not None
                                        else task["allowed_write_paths"]
                                    ),
                                )
                        else:
                            actual_changes = set(result.get("changed_files", [])) if "result" in locals() and isinstance(result, Mapping) else set()
                    except BaseException as exc:
                        attempt_error = exc
                        raise
                finally:
                    if attempt_error is not None and attempt_context is None:
                        _controller_cost_attempt(
                            task_id,
                            task,
                            role,
                            CODEX_EXEC_ROLE_CONTRACT
                            if getattr(runner, "is_live_model", False)
                            else NATIVE_SUBAGENT,
                            time.monotonic() - started_monotonic,
                            0,
                            runtime_usage,
                            retry_kind,
                            "FAILED",
                            state_root or WORKFLOW_STATE_ROOT,
                        )
            metric_run = dict(result) if isinstance(result, Mapping) else {}
            metric_run.update(
                {
                    "role": role,
                    "workflow_state": state,
                    "duration_seconds": time.monotonic() - started_monotonic,
                }
            )
            try:
                validate_role_result(role, result, actual_changes)
                validate_verification_package(
                    role,
                    task,
                    result,
                    construction_step=(construction_context.step if construction_context else None),
                )
            except BaseException:
                if attempt_context is None:
                    _controller_cost_attempt(
                        task_id,
                        task,
                        role,
                        CODEX_EXEC_ROLE_CONTRACT
                        if getattr(runner, "is_live_model", False)
                        else NATIVE_SUBAGENT,
                        metric_run["duration_seconds"],
                        0,
                        runtime_usage,
                        retry_kind,
                        "FAILED",
                        state_root or WORKFLOW_STATE_ROOT,
                        base_metric_run=metric_run,
                    )
                raise
            if attempt_context is None:
                _controller_cost_attempt(
                    task_id,
                    task,
                    role,
                    CODEX_EXEC_ROLE_CONTRACT
                    if getattr(runner, "is_live_model", False)
                    else NATIVE_SUBAGENT,
                    metric_run["duration_seconds"],
                    0,
                    runtime_usage,
                    retry_kind,
                    str(result.get("status", "UNKNOWN")),
                    state_root or WORKFLOW_STATE_ROOT,
                    base_metric_run=metric_run,
                )
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
                retry_kind = "technical"
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
    elif role in TERRA_WRITE_ROLES:
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
    elif role in {"sol_planner", "sol_medium_supervisor", "terra_xhigh_planner"}:
        target = "PLAN_READY"
    elif role in {"sol_reviewer", "sol_medium_reviewer", "terra_xhigh_reviewer"}:
        target = "ESCALATION_PROPOSED" if status == "ESCALATION_PROPOSED" else "REVIEW_READY"
    elif role in {"sol_xhigh", "sol_xhigh_planner"}:
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
    *,
    state_root: Path | None = None,
) -> str:
    result, state_after_retry = _run_role_with_technical_retry(
        store,
        task_id,
        task,
        state,
        role,
        runner,
        budget,
        state_root=state_root,
    )
    if result is None:
        return state_after_retry
    return _role_state_after_result(store, task_id, state_after_retry, role, result, budget)


def _load_enforced_read_only_route_role(
    store: WorkflowStore, task_id: str, task: Mapping[str, object]
) -> str:
    """Recover one executable role from a frozen enforced sol-only decision."""

    if task.get("task_type") not in {"PLAN", "ACCEPTANCE"}:
        _fail(
            "TERRA_OS_DECISION_REQUIRED",
            "generic terra_os read-only execution requires PLAN or ACCEPTANCE",
        )
    try:
        decision = load_artifact(store._require_task(task_id) / "route-decision.json")
    except ArtifactError as exc:
        raise WorkflowError(
            "TERRA_OS_DECISION_REQUIRED",
            "terra_os execution requires a persisted route decision",
        ) from exc
    validate_route_decision(decision)
    if (
        decision.get("task_id") != task_id
        or decision.get("task_sha256") != artifact_sha256(task)
        or decision.get("routing_mode") != "enforced"
        or decision.get("route") != "sol_only"
        or decision.get("rule_id")
        not in {
            "PLANNING_ONLY_ROUTE",
            "HIGH_RISK_READ_ONLY_ROUTE",
            "DECOMPOSABLE_READ_ONLY_ROUTE",
            "DECOMPOSABLE_SOL_ONLY_ROUTE",
        }
    ):
        _fail(
            "TERRA_OS_ROUTE_MISMATCH",
            "persisted route decision does not authorize this read-only execution",
        )
    route_events = [
        event
        for event in _load_event_records(store, task_id)
        if event.get("event_type") == "ROUTE_DECIDED"
    ]
    if len(route_events) != 1 or any(
        route_events[0].get(field) != decision[field]
        for field in (
            "task_sha256",
            "request_sha256",
            "route",
            "routing_mode",
            "rule_id",
        )
    ):
        _fail(
            "TERRA_OS_ROUTE_MISMATCH",
            "persisted route decision does not match its append-only event",
        )
    role = _terra_os_read_only_role(task)
    if role not in READ_ONLY_ROLES or role in TERRA_WRITE_ROLES:
        _fail("INVALID_ROLE", "terra_os route resolved to a non-read-only role")
    return role


def _load_enforced_construction_artifacts(
    store: WorkflowStore,
    task_id: str,
    construction_plan: object,
    request: object,
    step_id: object,
) -> tuple[dict[str, object], FrozenPlan, FrozenSubtask, str, dict[str, object]]:
    """Revalidate the exact plan and persisted enforced route before launch."""

    task = load_task(store._require_task(task_id) / "task.json")
    frozen = validate_plan(construction_plan, task)
    if frozen.task_id != task_id:
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction plan task_id does not match the task")
    if not isinstance(request, Mapping):
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction route request must be an object")
    request_value = dict(request)
    validate_route_request(request_value, task)
    try:
        stored_decision = load_artifact(store._require_task(task_id) / "route-decision.json")
    except ArtifactError as exc:
        raise WorkflowError("CONSTRUCTION_ROUTE_MISSING", "enforced construction requires a stored route decision") from exc
    validate_route_decision(stored_decision)
    recomputed = decide_route(
        task,
        request_value,
        "enforced",
        construction_plan=construction_plan,
        construction_step_id=step_id,
    )
    expected_wire = recomputed.to_dict()
    for field in ("task_id", "task_sha256", "request_sha256", "route", "rule_id", "routing_mode"):
        if stored_decision.get(field) != expected_wire[field]:
            _fail("CONSTRUCTION_ROUTE_MISMATCH", "stored route decision does not bind this construction launch")
    selected = next((candidate for candidate in frozen.tasks if candidate.id == step_id), None)
    if selected is None:
        _fail("CONSTRUCTION_CONTEXT_INVALID", "construction step is not present in the frozen plan")
    if selected.owner_role not in {"luna_construction", "terra_xhigh"}:
        _fail("CONSTRUCTION_OWNER_INVALID", "enforced construction owner is not executable")
    expected_role = selected.owner_role
    if recomputed.effective_roles != (expected_role,):
        _fail("CONSTRUCTION_ROUTE_MISMATCH", "frozen route owner is not eligible for this step")
    return task, frozen, selected, expected_role, expected_wire


def _construction_scope_sha256(step: FrozenSubtask) -> str:
    return artifact_sha256(
        {
            "read_scope": list(step.read_scope),
            "write_scope": list(step.write_scope),
            "do_not_touch": list(step.do_not_touch),
        }
    )


def _construction_authority(
    frozen: FrozenPlan,
    step: FrozenSubtask,
    role: str,
    route_wire: Mapping[str, object],
) -> dict[str, object]:
    return {
        "task_sha256": frozen.task_sha256,
        "plan_sha256": frozen.plan_sha256,
        "request_sha256": route_wire["request_sha256"],
        "route": route_wire["route"],
        "rule_id": route_wire["rule_id"],
        "routing_mode": route_wire["routing_mode"],
        "step_id": step.id,
        "role": role,
        "candidate_commit": frozen.candidate_commit,
        "scope_sha256": _construction_scope_sha256(step),
    }


def _frozen_construction_authority(store: WorkflowStore, task_id: str) -> dict[str, object]:
    records = [
        record for record in _load_event_records(store, task_id)
        if record.get("event_type") == "CONSTRUCTION_PLAN_FROZEN"
    ]
    if len(records) != 1:
        _fail("CONSTRUCTION_AUTHORITY_DRIFT", "construction authority must have exactly one freeze event")
    record = dict(records[0])
    record.pop("event_type", None)
    record.pop("timestamp_utc", None)
    expected = {
        "task_sha256", "plan_sha256", "request_sha256", "route", "rule_id",
        "routing_mode", "step_id", "role", "candidate_commit", "scope_sha256",
    }
    if set(record) != expected:
        _fail("CONSTRUCTION_AUTHORITY_DRIFT", "construction freeze event has invalid fields")
    return record


def _freeze_or_require_construction_plan(
    store: WorkflowStore,
    task_id: str,
    task: Mapping[str, object],
    frozen: FrozenPlan,
    step: FrozenSubtask,
    role: str,
    route_wire: Mapping[str, object],
    state: str,
) -> FrozenPlan:
    """Persist the plan before the owner gate and reject post-gate substitution."""

    plan_path = store._require_task(task_id) / "construction-plan.json"
    if plan_path.exists():
        try:
            recorded = load_artifact(plan_path)
        except ArtifactError as exc:
            raise WorkflowError(
                "CONSTRUCTION_PLAN_MISMATCH", "frozen construction plan cannot be read"
            ) from exc
        recorded_frozen = validate_plan(recorded, task)
        if recorded_frozen.plan_sha256 != frozen.plan_sha256:
            _fail(
                "CONSTRUCTION_PLAN_MISMATCH",
                "supplied construction plan differs from the owner-gated frozen plan",
            )
        authority = _construction_authority(recorded_frozen, step, role, route_wire)
        if _frozen_construction_authority(store, task_id) != authority:
            _fail("CONSTRUCTION_AUTHORITY_DRIFT", "construction state differs from first freeze")
        return recorded_frozen
    if state not in {"DRAFT", "TASK_VALIDATED"}:
        _fail(
            "CONSTRUCTION_PLAN_MISSING",
            "construction plan must be frozen before owner execution approval",
        )
    write_json_once(
        plan_path, frozen.to_dict(), conflict_code="CONSTRUCTION_PLAN_MISMATCH"
    )
    authority = _construction_authority(frozen, step, role, route_wire)
    store.append_event(
        task_id,
        {
            "event_type": "CONSTRUCTION_PLAN_FROZEN",
            "timestamp_utc": _utc_timestamp(),
            **authority,
        },
    )
    return frozen


def _construction_resume_document(
    request: Mapping[str, object], step_id: str, attempt: int
) -> dict[str, object]:
    return {
        "schema_version": "construction-resume-1",
        "request": dict(request),
        "step_id": step_id,
        "attempt": attempt,
    }


def _freeze_or_require_construction_resume(
    store: WorkflowStore,
    task_id: str,
    request: Mapping[str, object],
    step_id: str,
    attempt: int,
    state: str,
) -> dict[str, object]:
    """Persist only the validated values needed to resume an approved dispatch."""

    path = store._require_task(task_id) / "construction-resume.json"
    document = _construction_resume_document(request, step_id, attempt)
    if not path.exists():
        if state not in {"DRAFT", "TASK_VALIDATED"}:
            _fail(
                "CONSTRUCTION_CONTEXT_MISSING",
                "construction resume context must be frozen before owner approval",
            )
        write_json_once(
            path, document, conflict_code="CONSTRUCTION_CONTEXT_MISMATCH"
        )
        store.append_event(
            task_id,
            {
                "event_type": "CONSTRUCTION_RESUME_CONTEXT_FROZEN",
                "timestamp_utc": _utc_timestamp(),
                "context_sha256": artifact_sha256(document),
            },
        )
        return document
    try:
        recorded = load_artifact(path)
    except ArtifactError as exc:
        raise WorkflowError(
            "CONSTRUCTION_CONTEXT_MISMATCH",
            "construction resume context cannot be read",
        ) from exc
    if recorded == document:
        if state == "REWORK_AUTHORIZED" and not any(
            event.get("event_type") == "CONSTRUCTION_RESUME_CONTEXT_UPDATED"
            and event.get("context_sha256") == artifact_sha256(document)
            for event in _load_event_records(store, task_id)
        ):
            _fail(
                "CONSTRUCTION_CONTEXT_MISMATCH",
                "rework dispatch must advance to the next attempt",
            )
        return document
    if (
        state != "REWORK_AUTHORIZED"
        or recorded.get("request") != document["request"]
        or recorded.get("step_id") != step_id
        or not isinstance(recorded.get("attempt"), int)
        or attempt != recorded["attempt"] + 1
    ):
        _fail(
            "CONSTRUCTION_CONTEXT_MISMATCH",
            "construction resume context differs from frozen authority",
        )
    previous_sha256 = artifact_sha256(recorded)
    atomic_write_json(path, document)
    store.append_event(
        task_id,
        {
            "event_type": "CONSTRUCTION_RESUME_CONTEXT_UPDATED",
            "timestamp_utc": _utc_timestamp(),
            "previous_context_sha256": previous_sha256,
            "context_sha256": artifact_sha256(document),
        },
    )
    return document


def _record_or_recover_enforced_dispatch(
    store: WorkflowStore,
    task_id: str,
    frozen: FrozenPlan,
    step: FrozenSubtask,
    attempt: int,
    route_wire: Mapping[str, object],
    role: str,
    *,
    record_missing: bool = True,
) -> str:
    route_fields = {
        field: route_wire[field] for field in ("route", "rule_id", "routing_mode")
    }
    identity = dispatch_id(
        frozen.plan_sha256,
        frozen.task_sha256,
        step.id,
        attempt,
        str(frozen.candidate_commit),
        request_sha256=str(route_wire["request_sha256"]),
        route_fields=route_fields,
        role=role,
    )
    expected = {
        "event_type": "DISPATCH_RECORDED",
        "owner_task_id": step.id,
        "owner_role": step.owner_role,
        "plan_sha256": frozen.plan_sha256,
        "task_sha256": frozen.task_sha256,
        "scope_sha256": artifact_sha256(
            {
                "read_scope": list(step.read_scope),
                "write_scope": list(step.write_scope),
                "do_not_touch": list(step.do_not_touch),
            }
        ),
        "subtask_id": step.id,
        "attempt": attempt,
        "candidate_commit": frozen.candidate_commit,
        "request_sha256": route_wire["request_sha256"],
        "route_fields": route_fields,
        "role": role,
        "dispatch_id": identity,
    }
    ledger = store._require_task(task_id) / "dispatches.jsonl"
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise WorkflowError("DISPATCH_READ_ERROR", "cannot read dispatch ledger") from exc
    recovered = False
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                "DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains invalid JSON"
            ) from exc
        if not isinstance(record, Mapping):
            _fail("DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains an invalid record")
        same_attempt = (
            record.get("subtask_id") == step.id and record.get("attempt") == attempt
        )
        if record.get("dispatch_id") == identity or same_attempt:
            if dict(record) != expected or recovered:
                _fail(
                    "ORPHAN_DISPATCH_MISMATCH",
                    "existing dispatch record does not match this construction attempt",
                )
            recovered = True
        elif (
            record.get("subtask_id") == step.id
            and isinstance(record.get("attempt"), int)
            and int(record["attempt"]) > attempt
        ):
            _fail(
                "ORPHAN_DISPATCH_MISMATCH",
                "dispatch ledger contains a later construction attempt",
            )
    if recovered:
        return identity
    if not record_missing:
        return identity
    return record_dispatch(
        store,
        task_id,
        frozen,
        step.id,
        attempt,
        str(frozen.candidate_commit),
        store_locked=True,
        request_sha256=str(route_wire["request_sha256"]),
        route_fields=route_fields,
        role=role,
    )


def run_enforced_construction(
    task_id: str,
    *,
    construction_plan: object,
    request: object,
    step_id: object,
    attempt: int,
    runner: Runner,
    allow_live_model: bool,
    state_root: Path | None = None,
) -> str:
    """Run exactly one approved construction step; never infer it from route wire data."""

    if not isinstance(allow_live_model, bool):
        _fail("INVALID_LIVE_MODEL_FLAG", "allow_live_model must be a boolean")
    if not hasattr(runner, "run_construction"):
        _fail("CONSTRUCTION_CONTEXT_INVALID", "runner must provide run_construction for a frozen step")
    if getattr(runner, "is_live_model", False) and not allow_live_model:
        _fail("LIVE_MODEL_NOT_AUTHORIZED", "live model execution requires explicit authorization")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        _fail("DISPATCH_IDENTITY_DRIFT", "construction attempt must be a positive integer")
    store = WorkflowStore(state_root or WORKFLOW_STATE_ROOT)
    with store.lock(task_id):
        if repair_ledger_claims_task(store, task_id):
            _fail(
                "REPAIR_ADAPTER_REQUIRED",
                "adversarial-acceptance-1 tasks require the verified assignment adapter",
            )
        task, frozen, step, role, route_wire = _load_enforced_construction_artifacts(
            store, task_id, construction_plan, request, step_id
        )
        state = _current_state(store, task_id)
        budget = _budget_from_events(store, task_id)
        frozen = _freeze_or_require_construction_plan(
            store, task_id, task, frozen, step, role, route_wire, state
        )
        _freeze_or_require_construction_resume(
            store,
            task_id,
            dict(request),
            step.id,
            attempt,
            state,
        )
        step = next(candidate for candidate in frozen.tasks if candidate.id == step.id)
        if state in {
            "BLOCKED",
            "CLOSED",
            "ABORTED",
            "DEFERRED",
            "AWAITING_OWNER_DECISION",
            "IMPLEMENTED_CANDIDATE",
        }:
            return state
        if state == "DRAFT":
            state = _transition(store, task_id, state, "TASK_VALIDATED", budget)
        if state == "TASK_VALIDATED":
            return _transition(
                store,
                task_id,
                state,
                "AWAITING_OWNER_DECISION",
                budget,
                event_type="CONSTRUCTION_OWNER_GATE_REACHED",
            )
        if state == "APPROVED_FOR_EXECUTION":
            if not _authorization_is_recorded(store, task_id, state, "approve_execution"):
                return state
            if getattr(runner, "is_live_model", False):
                create_worktree(task, owner_authorized=True, store=store)
            state = _transition(
                store,
                task_id,
                state,
                "WORKTREE_READY",
                budget,
                owner_authorized=True,
            )
        if state == "REWORK_AUTHORIZED":
            if not _authorization_is_recorded(store, task_id, state, "authorize_rework"):
                return state
        if state not in {
            "WORKTREE_READY",
            "REWORK_AUTHORIZED",
            "IMPLEMENTATION_RUNNING",
        }:
            _fail("CONSTRUCTION_STATE_INVALID", "construction step is not in an implementation state")
        authority = _construction_authority(frozen, step, role, route_wire)
        if _frozen_construction_authority(store, task_id) != authority:
            _fail("CONSTRUCTION_AUTHORITY_DRIFT", "launch differs from the owner-frozen authority")
        owner_decision = _load_latest_decision(store, task_id)
        if not owner_decision or owner_decision.get("construction_authority_sha256") != artifact_sha256(authority):
            _fail("CONSTRUCTION_AUTHORITY_DRIFT", "owner decision is not bound to frozen construction authority")
        if has_active_repair_assignment(store, task_id):
            if state == "WORKTREE_READY":
                state = _transition(
                    store, task_id, state, "IMPLEMENTATION_RUNNING", budget
                )
            elif state == "REWORK_AUTHORIZED":
                state = _transition(
                    store,
                    task_id,
                    state,
                    "IMPLEMENTATION_RUNNING",
                    budget,
                    owner_authorized=True,
                )
            return _transition(
                store,
                task_id,
                state,
                "BLOCKED",
                budget,
                event_type="REPAIR_EXECUTION_INTEGRATION_BLOCKED",
            )
        launch_id = _record_or_recover_enforced_dispatch(
            store, task_id, frozen, step, attempt, route_wire, role
        )
        if state == "WORKTREE_READY":
            state = _transition(
                store, task_id, state, "IMPLEMENTATION_RUNNING", budget
            )
        elif state == "REWORK_AUTHORIZED":
            state = _transition(
                store,
                task_id,
                state,
                "IMPLEMENTATION_RUNNING",
                budget,
                owner_authorized=True,
            )
        context = ConstructionExecutionContext(
            plan=frozen,
            step=step,
            dispatch_id=launch_id,
            task_sha256=frozen.task_sha256,
            request_sha256=str(route_wire["request_sha256"]),
            role=role,
        )
        result, state_after_retry = _run_role_with_technical_retry(
            store,
            task_id,
            task,
            state,
            role,
            runner,
            budget,
            construction_context=context,
            state_root=store.root,
        )
        if result is None:
            return state_after_retry
        return _role_state_after_result(
            store, task_id, state_after_retry, role, result, budget
        )


def run_until_gate(
    task_id: str,
    *,
    runner: Runner,
    allow_live_model: bool,
    construction_plan: object | None = None,
    construction_request: object | None = None,
    construction_step_id: object = None,
    construction_attempt: int | None = None,
    state_root: Path | None = None,
) -> str:
    """Advance one bounded pipeline only until its next owner-controlled gate."""

    store = WorkflowStore(state_root or WORKFLOW_STATE_ROOT)
    # A v2 acceptance ledger owns every phase of its task, including an open,
    # failed, accepted, or terminal attempt.  The generic state machine has no
    # receipt/capability channel, so it must not even select a role for it.
    if repair_ledger_claims_task(store, task_id):
        _fail(
            "REPAIR_ADAPTER_REQUIRED",
            "adversarial-acceptance-1 tasks require the verified assignment adapter",
        )
    construction_values = (
        construction_plan,
        construction_request,
        construction_step_id,
        construction_attempt,
    )
    if any(value is not None for value in construction_values):
        if any(value is None for value in construction_values):
            _fail("CONSTRUCTION_CONTEXT_INVALID", "construction plan, request, step, and attempt are required together")
        return run_enforced_construction(
            task_id,
            construction_plan=construction_plan,
            request=construction_request,
            step_id=construction_step_id,
            attempt=construction_attempt,
            runner=runner,
            allow_live_model=allow_live_model,
            state_root=state_root,
        )
    if not isinstance(allow_live_model, bool):
        _fail("INVALID_LIVE_MODEL_FLAG", "allow_live_model must be a boolean")
    if not hasattr(runner, "run"):
        _fail("INVALID_RUNNER", "runner must provide run(role, task)")
    if getattr(runner, "is_live_model", False) and not allow_live_model:
        _fail("LIVE_MODEL_NOT_AUTHORIZED", "live model execution requires explicit authorization")
    with store.lock(task_id):
        if repair_ledger_claims_task(store, task_id):
            _fail(
                "REPAIR_ADAPTER_REQUIRED",
                "adversarial-acceptance-1 tasks require the verified assignment adapter",
            )
        task_path = store._require_task(task_id) / "task.json"
        task = load_task(task_path)
        state = _current_state(store, task_id)
        budget = _budget_from_events(store, task_id)
        config = _load_workflow_config()
        enforced_read_only_role: str | None = None
        if (
            _configured_routing_mode(config) == "enforced"
            and _resolve_role_policy(config) == "terra_os"
        ):
            if state == "IMPLEMENTATION_RUNNING" and has_active_repair_assignment(store, task_id):
                return _transition(
                    store,
                    task_id,
                    state,
                    "BLOCKED",
                    budget,
                    event_type="REPAIR_EXECUTION_INTEGRATION_BLOCKED",
                )
            if task["task_type"] in {"PLAN", "ACCEPTANCE"}:
                enforced_read_only_role = _load_enforced_read_only_route_role(
                    store, task_id, task
                )
            else:
                _fail(
                    "CONSTRUCTION_CONTEXT_REQUIRED",
                    "terra_os remediation requires a validated construction context",
                )
        while True:
            if getattr(runner, "is_live_model", False) and task["task_type"] == "ACCEPTANCE":
                assert_acceptance_candidate(task, _execution_repo(task, "luna"))
            if state in {"BLOCKED", "CLOSED", "ABORTED", "DEFERRED", "AWAITING_OWNER_DECISION"}:
                return state
            if state == "DRAFT":
                state = _transition(store, task_id, state, "TASK_VALIDATED", budget)
                continue
            if state == "TASK_VALIDATED":
                if enforced_read_only_role is not None:
                    state = _transition(
                        store, task_id, state, "PLAN_OR_REVIEW_RUNNING", budget
                    )
                    continue
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
                if has_active_repair_assignment(store, task_id):
                    return _transition(
                        store,
                        task_id,
                        state,
                        "BLOCKED",
                        budget,
                        event_type="REPAIR_EXECUTION_INTEGRATION_BLOCKED",
                    )
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
                role = (
                    enforced_read_only_role
                    if enforced_read_only_role is not None
                    and not _authorization_is_recorded(
                        store,
                        task_id,
                        "ESCALATION_AUTHORIZED",
                        "authorize_escalation",
                    )
                    else _role_for_plan_or_review(store, task_id, task)
                )
                state = _run_pipeline_role(
                    store, task_id, task, state, role, runner, budget,
                    state_root=store.root,
                )
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
        if decision in {"approve_execution", "authorize_rework"}:
            try:
                authority = _frozen_construction_authority(store, task_id)
            except WorkflowError:
                pass
            else:
                record["construction_authority_sha256"] = artifact_sha256(authority)
        store.record_decision(task_id, record)
        event = dict(record)
        event["retry_budget"] = _budget_record(budget)
        store.append_event(task_id, event)
        return target


def _load_construction_resume(
    store: WorkflowStore, task_id: str
) -> tuple[object, Mapping[str, object], str, int] | None:
    task_dir = store._require_task(task_id)
    context_path = task_dir / "construction-resume.json"
    if not context_path.exists():
        return None
    try:
        context = load_artifact(context_path)
        plan = load_artifact(task_dir / "construction-plan.json")
    except ArtifactError as exc:
        raise WorkflowError(
            "CONSTRUCTION_CONTEXT_MISMATCH",
            "frozen construction resume artifacts cannot be read",
        ) from exc
    if (
        set(context) != {"schema_version", "request", "step_id", "attempt"}
        or context.get("schema_version") != "construction-resume-1"
        or not isinstance(context.get("request"), Mapping)
        or not isinstance(context.get("step_id"), str)
        or not context["step_id"]
        or isinstance(context.get("attempt"), bool)
        or not isinstance(context.get("attempt"), int)
        or context["attempt"] < 1
    ):
        _fail(
            "CONSTRUCTION_CONTEXT_MISMATCH",
            "frozen construction resume context is invalid",
        )
    return (
        plan,
        dict(context["request"]),
        context["step_id"],
        context["attempt"],
    )


def _resume_enabled(config: Mapping[str, object]) -> bool:
    automation = config.get("automation")
    return (
        isinstance(automation, Mapping)
        and automation.get("allow_decide_resume") is True
    )


def _assert_resume_dispatch_available(
    store: WorkflowStore,
    task_id: str,
    context: tuple[object, Mapping[str, object], str, int],
) -> None:
    plan, request, step_id, attempt = context
    _, frozen, step, role, route_wire = _load_enforced_construction_artifacts(
        store, task_id, plan, request, step_id
    )
    _record_or_recover_enforced_dispatch(
        store,
        task_id,
        frozen,
        step,
        attempt,
        route_wire,
        role,
        record_missing=False,
    )


def _prepare_resume(
    store: WorkflowStore,
    task_id: str,
    args: argparse.Namespace,
    *,
    decision: str | None = None,
) -> tuple[
    Runner,
    tuple[object, Mapping[str, object], str, int] | None,
]:
    context = _load_construction_resume(store, task_id)
    state = _current_state(store, task_id)
    events = _load_event_records(store, task_id)
    latest_rework_authorization = max(
        (
            index
            for index, event in enumerate(events)
            if event.get("event_type") == "OWNER_DECISION"
            and event.get("decision") == "authorize_rework"
        ),
        default=-1,
    )
    latest_matching_context_update = -1
    if context is not None:
        _plan, request, step_id, attempt = context
        current_digest = artifact_sha256(
            _construction_resume_document(request, step_id, attempt)
        )
        latest_matching_context_update = max(
            (
                index
                for index, event in enumerate(events)
                if event.get("event_type") == "CONSTRUCTION_RESUME_CONTEXT_UPDATED"
                and event.get("context_sha256") == current_digest
            ),
            default=-1,
        )
    context_was_advanced = (
        latest_matching_context_update > latest_rework_authorization
    )
    if context is not None and (
        decision == "authorize_rework"
        or (state == "REWORK_AUTHORIZED" and not context_was_advanced)
    ):
        plan, request, step_id, attempt = context
        context = (plan, request, step_id, attempt + 1)
    if context is not None and (
        state in {"APPROVED_FOR_EXECUTION", "WORKTREE_READY", "REWORK_AUTHORIZED"}
        or decision in {"approve_execution", "authorize_rework"}
    ):
        _assert_resume_dispatch_available(store, task_id, context)
    if args.runner == "fake":
        return FakeRunner(), context
    if not args.allow_live_model:
        _fail(
            "LIVE_MODEL_NOT_AUTHORIZED",
            "--allow-live-model is required for a live resume",
        )
    if context is None:
        _fail(
            "LIVE_RESUME_CONTEXT_REQUIRED",
            "live resume requires a frozen construction dispatch",
        )
    sessions = args.runtime_sessions_dir
    if sessions is None or not sessions.is_absolute() or not sessions.is_dir():
        _fail(
            "RUNTIME_SESSIONS_DIR_INVALID",
            "live resume requires an existing absolute --runtime-sessions-dir",
        )
    return CodexConstructionRunner(store.root, sessions), context


def _resume_stored_task(
    store: WorkflowStore,
    task_id: str,
    runner: Runner,
    context: tuple[object, Mapping[str, object], str, int] | None,
) -> str:
    if context is not None:
        plan, request, step_id, attempt = context
        return run_enforced_construction(
            task_id,
            construction_plan=plan,
            request=request,
            step_id=step_id,
            attempt=attempt,
            runner=runner,
            allow_live_model=getattr(runner, "is_live_model", False),
            state_root=store.root,
        )
    return run_until_gate(
        task_id,
        runner=runner,
        allow_live_model=False,
        state_root=store.root,
    )


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

    team_call = sub.add_parser("team-call")
    team_call.add_argument("message")
    team_call.add_argument("--root", type=Path)
    team_call.add_argument("--repository-root", type=Path, required=True)
    team_call.add_argument("--runner", choices=("fake", "live"), default="fake")
    team_call.add_argument("--allow-live-model", action="store_true")
    team_call.add_argument(
        "--runtime-sessions-dir",
        type=Path,
        metavar="ABSOLUTE_DIR",
        help="required for explicitly authorized live Team Call execution",
    )

    run = sub.add_parser("run")
    run.add_argument("task_path", nargs="?", type=Path)
    run.add_argument("--task", dest="task_option", type=Path)
    run.add_argument("--runner", choices=("fake", "live"), default=None)
    run.add_argument("--allow-live-model", action="store_true")
    run.add_argument("--role", default="luna", choices=tuple(FAKE_ROLE_RESULTS))
    run.add_argument(
        "--construction-plan",
        type=Path,
        help="required with the construction request, step, and attempt for enforced construction",
    )
    run.add_argument(
        "--construction-request",
        type=Path,
        help="hash-bound ai-route-request-1 used for the stored enforced decision",
    )
    run.add_argument("--construction-step", help="one frozen plan subtask id")
    run.add_argument("--attempt", type=int, help="positive deterministic construction attempt")
    run.add_argument(
        "--runtime-sessions-dir",
        type=Path,
        metavar="ABSOLUTE_DIR",
        help="required with --runner live; an existing absolute Codex sessions directory",
    )
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
    decide.add_argument("--resume", action="store_true")
    decide.add_argument("--runner", choices=("fake", "live"), default="fake")
    decide.add_argument("--allow-live-model", action="store_true")
    decide.add_argument("--runtime-sessions-dir", type=Path, metavar="ABSOLUTE_DIR")

    route_command = sub.add_parser("route")
    route_command.add_argument("--task", type=Path, required=True)
    route_command.add_argument("--request", type=Path, required=True)
    route_command.add_argument(
        "--mode",
        choices=("legacy", "shadow", "enforced"),
        default=_configured_routing_mode(_load_workflow_config()),
    )
    route_command.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    schedule_batch = sub.add_parser("schedule-batch")
    schedule_batch.add_argument("--task", type=Path, required=True)
    schedule_batch.add_argument("--plan", type=Path, required=True)
    schedule_batch.add_argument(
        "--root", type=Path, default=Path("data/state/ai-workflow")
    )

    schedule_result = sub.add_parser("schedule-result")
    schedule_result.add_argument("task_id")
    schedule_result.add_argument("--plan", type=Path, required=True)
    schedule_result.add_argument("--dispatch-id", required=True)
    schedule_result.add_argument("--result", type=Path, required=True)
    schedule_result.add_argument(
        "--root", type=Path, default=Path("data/state/ai-workflow")
    )

    schedule_receipt = sub.add_parser("schedule-receipt")
    schedule_receipt.add_argument("task_id")
    schedule_receipt.add_argument("--plan", type=Path, required=True)
    schedule_receipt.add_argument("--receipt", type=Path, required=True)
    schedule_receipt.add_argument(
        "--root", type=Path, default=Path("data/state/ai-workflow")
    )

    schedule_final = sub.add_parser("schedule-final")
    schedule_final.add_argument("task_id")
    schedule_final.add_argument("--plan", type=Path, required=True)
    schedule_final.add_argument("--acceptance-task-id", required=True)
    schedule_final.add_argument("--candidate-commit", required=True)
    schedule_final.add_argument("--owner-receipt", type=Path)
    schedule_final.add_argument("--acceptor", type=Path)
    schedule_final.add_argument(
        "--root", type=Path, default=Path("data/state/ai-workflow")
    )

    resume = sub.add_parser("resume")
    resume.add_argument("task_id")
    resume.add_argument("--runner", choices=("fake", "live"), default="fake")
    resume.add_argument("--allow-live-model", action="store_true")
    resume.add_argument("--runtime-sessions-dir", type=Path, metavar="ABSOLUTE_DIR")
    resume.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))

    abort = sub.add_parser("abort")
    abort.add_argument("task_id")
    abort.add_argument("--by", default="owner")
    abort.add_argument("--root", type=Path, default=Path("data/state/ai-workflow"))
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
        state_root=args.root,
        runtime_evidence_required=True,
        runtime_sessions_dir=args.runtime_sessions_dir,
    )
    prompt_result = build_role_prompt_result(
        "luna",
        task,
        contract,
        _authoritative_evidence_paths(task),
        state_root=args.root,
    )
    return run_codex("luna", task, prompt_result.prompt, paths, prompt_result=prompt_result)


def _scheduler_runtime():
    try:
        from . import ai_workflow_scheduler as scheduler
    except ImportError:
        import ai_workflow_scheduler as scheduler
    return scheduler


def _load_scheduler_cli_plan(
    store: WorkflowStore,
    task_id: str,
    plan_path: Path,
) -> FrozenPlan:
    try:
        plan = load_artifact(plan_path)
    except ArtifactError as exc:
        raise WorkflowError(exc.code, exc.message) from exc
    task = load_task(store._require_task(task_id) / "task.json")
    return validate_plan(plan, task)


def _scheduler_receipt_status(role: str, status: object) -> str:
    if role == "luna":
        mapped = {
            "SUPPORTED": "IMPLEMENTED_CANDIDATE",
            "PARTIALLY_SUPPORTED": "NEEDS_CLARIFICATION",
            "BLOCKED": "BLOCKED",
        }.get(status)
    else:
        mapped = status if status in {
            "IMPLEMENTED_CANDIDATE",
            "NEEDS_CLARIFICATION",
            "BLOCKED",
        } else None
    if not isinstance(mapped, str):
        _fail("RECEIPT_RESULT_IDENTITY_MISMATCH", "result status cannot form a scheduler receipt")
    return mapped


def _schedule_result(
    store: WorkflowStore,
    frozen: FrozenPlan,
    dispatch_identity: str,
    source_path: Path,
) -> dict[str, object]:
    scheduler = _scheduler_runtime()
    replay = scheduler.replay_scheduler(store, frozen)
    dispatch = replay.dispatches.get(dispatch_identity)
    if dispatch is None:
        _fail("DISPATCH_IDENTITY_DRIFT", "scheduler result references an unknown dispatch")
    try:
        result = load_artifact(source_path)
    except ArtifactError as exc:
        raise WorkflowError(exc.code, exc.message) from exc
    role = str(dispatch["owner_role"])
    identity = {
        "dispatch_id": dispatch_identity,
        "task_id": frozen.task_id,
        "step_id": dispatch["subtask_id"],
        "attempt": dispatch["attempt"],
    }
    for field, expected in identity.items():
        if field in result and result[field] != expected:
            _fail(
                "RECEIPT_RESULT_IDENTITY_MISMATCH",
                f"scheduler result {field} does not match dispatch",
            )
        result[field] = expected
    changed = result.get("changed_files")
    if not isinstance(changed, list) or any(not isinstance(item, str) for item in changed):
        _fail("INVALID_ROLE_RESULT", "scheduler result changed_files is invalid")
    validate_role_result(role, result, set(changed))
    result_path = (
        store._require_task(frozen.task_id)
        / "scheduler-results"
        / f"{dispatch_identity}.json"
    )
    digest = write_json_once(
        result_path,
        result,
        conflict_code="RECEIPT_RESULT_CONFLICT",
    )
    return {
        "schema_version": "construction-receipt-1",
        "task_id": frozen.task_id,
        "subtask_id": dispatch["subtask_id"],
        "dispatch_id": dispatch_identity,
        "plan_sha256": frozen.plan_sha256,
        "task_sha256": frozen.task_sha256,
        "candidate_commit": frozen.candidate_commit,
        "result_sha256": digest,
        "status": _scheduler_receipt_status(role, result.get("status")),
    }


def _run_command(args: argparse.Namespace) -> int:
    if args.command == "schedule-batch":
        store = WorkflowStore(args.root)
        task = load_task(args.task)
        stored = load_task(store._require_task(task["task_id"]) / "task.json")
        if task != stored:
            _fail("TASK_STORE_MISMATCH", "scheduler task input does not match stored task")
        frozen = _load_scheduler_cli_plan(store, task["task_id"], args.plan)
        proposals = _scheduler_runtime().dispatch_ready_batch(store, frozen)
        print(_canonical_json(list(proposals)))
        return 0
    if args.command == "schedule-result":
        store = WorkflowStore(args.root)
        frozen = _load_scheduler_cli_plan(store, args.task_id, args.plan)
        receipt = _schedule_result(
            store,
            frozen,
            args.dispatch_id,
            args.result,
        )
        print(_canonical_json(receipt))
        return 0
    if args.command == "schedule-receipt":
        store = WorkflowStore(args.root)
        frozen = _load_scheduler_cli_plan(store, args.task_id, args.plan)
        try:
            receipt = load_artifact(args.receipt)
        except ArtifactError as exc:
            raise WorkflowError(exc.code, exc.message) from exc
        event = _scheduler_runtime().record_step_receipt(store, frozen, receipt)
        print(_canonical_json(event))
        return 0
    if args.command == "schedule-final":
        store = WorkflowStore(args.root)
        frozen = _load_scheduler_cli_plan(store, args.task_id, args.plan)
        scheduler = _scheduler_runtime()
        child = scheduler.create_final_acceptance_case(
            store,
            frozen,
            args.acceptance_task_id,
            args.candidate_commit,
        )
        issue_values = (args.owner_receipt, args.acceptor)
        if any(value is not None for value in issue_values):
            if any(value is None for value in issue_values):
                _fail(
                    "ACCEPTANCE_INPUT_INVALID",
                    "--owner-receipt and --acceptor are required together",
                )
            try:
                owner_document = load_artifact(args.owner_receipt)
                acceptor_document = load_artifact(args.acceptor)
            except ArtifactError as exc:
                raise WorkflowError(exc.code, exc.message) from exc
            try:
                owner = VerifiedActorReceipt(**owner_document)
                acceptor = ActorIdentity(**acceptor_document)
            except (TypeError, RuntimeError) as exc:
                raise WorkflowError(
                    "ACCEPTANCE_INPUT_INVALID",
                    "final acceptance identity artifact is invalid",
                ) from exc
            assignment = scheduler.issue_final_acceptance(
                store,
                frozen,
                child["task_id"],
                owner,
                acceptor,
            )
            print(
                _canonical_json(
                    {
                        "task_id": assignment.task_id,
                        "assignment_id": assignment.assignment_id,
                        "phase": assignment.phase,
                    }
                )
            )
            return 0
        print(_canonical_json(child))
        return 0
    if args.command == "resume":
        store = WorkflowStore(args.root)
        runner, context = _prepare_resume(store, args.task_id, args)
        print(_resume_stored_task(store, args.task_id, runner, context))
        return 0
    if args.command == "abort":
        store = WorkflowStore(args.root)
        print(_apply_owner_decision(store, args.task_id, "abort", args.by))
        return 0
    if args.command == "team-call":
        state_root = (
            Path(args.root)
            if args.root is not None
            else _default_team_call_state_root(args.repository_root)
        )
        if args.runner == "live":
            if not args.allow_live_model:
                _fail("LIVE_MODEL_NOT_AUTHORIZED", "--allow-live-model is required for the live runner")
            try:
                call = parse_team_call(args.message)
                if call is None:
                    _fail("TEAM_CALL_INVALID", "message must start with a team call directive")
                intent = classify_team_call(call)
            except TeamCallError as exc:
                raise _team_call_error_as_workflow(exc) from exc
            controller: TeamCallController = TeamCallProductionController(
                state_root,
                allow_live_model=True,
                runtime_sessions_dir=args.runtime_sessions_dir,
            )
        else:
            controller = TeamCallFakeController()
        receipt = run_team_call(
            args.message,
            repository_root=args.repository_root,
            state_root=state_root,
            controller=controller,
        )
        print(_canonical_json({
            "call_id": receipt.call_id,
            "raw_request_sha256": receipt.raw_request_sha256,
            "intake_sha256": receipt.intake_sha256,
            "disposition": receipt.disposition,
            "risk_reasons": list(receipt.risk_reasons),
            "task_id": receipt.task_id,
            "created_at_utc": receipt.created_at_utc,
            "result_sha256": receipt.result_sha256,
        }))
        return 2 if receipt.disposition == "BLOCKED" else 0
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
    if args.command == "route":
        task = load_task(args.task)
        try:
            request = load_artifact(args.request)
        except ArtifactError as exc:
            raise WorkflowError(exc.code, exc.message) from exc
        computed = decide_route(task, request, args.mode)
        store = WorkflowStore(args.root)
        decision = persist_or_reuse_route_decision(store, task["task_id"], computed)
        record_route_advice(
            store,
            task["task_id"],
            evaluate_and_apply_route_advice(
                decision,
                recommended_route=decision.route,
                state_root=args.root,
                task=task,
                request=request,
            ),
            request_sha256=artifact_sha256(request),
        )
        print(_canonical_json(decision.to_dict()))
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
        store = WorkflowStore(args.root)
        if decision == "authorize_final_xhigh":
            if args.resume:
                _fail(
                    "DECIDE_RESUME_INVALID",
                    "authorize_final_xhigh cannot be combined with --resume",
                )
            try:
                from .ai_workflow_repairs import authorize_final_xhigh
            except ImportError:
                from ai_workflow_repairs import authorize_final_xhigh
            authorize_final_xhigh(store, args.task_id, args.by)
            print("DECISION_RECORDED")
            return 0
        prepared: tuple[
            Runner,
            tuple[object, Mapping[str, object], str, int] | None,
        ] | None = None
        if args.resume:
            if not _resume_enabled(_load_workflow_config()):
                _fail(
                    "DECIDE_RESUME_DISABLED",
                    "decide --resume is disabled by workflow policy",
                )
            prepared = _prepare_resume(
                store, args.task_id, args, decision=decision
            )
        _apply_owner_decision(store, args.task_id, decision, args.by)
        print("DECISION_RECORDED")
        if prepared is not None:
            runner, context = prepared
            print(_resume_stored_task(store, args.task_id, runner, context))
        return 0
    if args.command == "run":
        construction_values = (
            args.construction_plan,
            args.construction_request,
            args.construction_step,
            args.attempt,
        )
        if any(value is not None for value in construction_values):
            if any(value is None for value in construction_values):
                _fail(
                    "CONSTRUCTION_CONTEXT_INVALID",
                    "--construction-plan, --construction-request, --construction-step, and --attempt are required together",
                )
            if args.runner == "fake":
                construction_runner: Runner = FakeRunner()
            elif args.runner == "live":
                if not args.allow_live_model:
                    _fail(
                        "LIVE_MODEL_NOT_AUTHORIZED",
                        "--allow-live-model is required for the live runner",
                    )
                construction_runner = CodexConstructionRunner(
                    args.root, args.runtime_sessions_dir
                )
            else:
                _fail(
                    "NOT_IMPLEMENTED_IN_CURRENT_STAGE",
                    "enforced construction requires --runner fake or --runner live",
                )
            try:
                construction_plan = load_artifact(args.construction_plan)
                construction_request = load_artifact(args.construction_request)
            except ArtifactError as exc:
                raise WorkflowError(exc.code, exc.message) from exc
            task = load_task(_task_path_from_args(args))
            stored_task = load_task(
                WorkflowStore(args.root)._require_task(task["task_id"]) / "task.json"
            )
            if stored_task != task:
                _fail("TASK_STORE_MISMATCH", "construction task input does not match the stored task")
            state = run_enforced_construction(
                task["task_id"],
                construction_plan=construction_plan,
                request=construction_request,
                step_id=args.construction_step,
                attempt=args.attempt,
                runner=construction_runner,
                allow_live_model=args.allow_live_model,
                state_root=args.root,
            )
            print(state)
            return 0
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
        stored_task = load_task(
            WorkflowStore(args.root)._require_task(task["task_id"]) / "task.json"
        )
        if stored_task != task:
            _fail("TASK_STORE_MISMATCH", "run task input does not match the stored task")
        state = run_until_gate(
            task["task_id"],
            runner=FakeRunner(),
            allow_live_model=False,
            state_root=args.root,
        )
        print(state)
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


try:
    from .ai_workflow_repairs import (
        ActorIdentity,
        AcceptanceAssignment,
        AdversarialEvidence,
        AssignmentCapability,
        ControllerAssignmentBoundary,
        ControllerExecutionAttestation,
        RepairAssignment,
        RepairFinding,
        VerifiedActorReceipt,
        assign_repair,
        complete_acceptance_assignment,
        execute_adversarial_evidence,
        has_active_repair_assignment,
        issue_acceptance_assignment,
        open_task_acceptance,
        record_adversarial_review,
        record_repair_assignment,
        record_repair_completion,
        record_repair_review,
        record_sol_repair_authorization,
        repair_ledger_claims_task,
        replay_acceptance_ledger,
        run_assignment,
        validate_repair_result,
    )
except ImportError:  # direct script execution
    from ai_workflow_repairs import (
        ActorIdentity,
        AcceptanceAssignment,
        AdversarialEvidence,
        AssignmentCapability,
        ControllerAssignmentBoundary,
        ControllerExecutionAttestation,
        RepairAssignment,
        RepairFinding,
        VerifiedActorReceipt,
        assign_repair,
        complete_acceptance_assignment,
        execute_adversarial_evidence,
        has_active_repair_assignment,
        issue_acceptance_assignment,
        open_task_acceptance,
        record_adversarial_review,
        record_repair_assignment,
        record_repair_completion,
        record_repair_review,
        record_sol_repair_authorization,
        repair_ledger_claims_task,
        replay_acceptance_ledger,
        run_assignment,
        validate_repair_result,
    )


if __name__ == "__main__":
    raise SystemExit(main())
