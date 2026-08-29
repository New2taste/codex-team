"""Host-static role preflight with internally recaptured context."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from .ai_workflow_artifacts import (
        PROCESS_GENERATION,
        ROLES,
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        canonical_json,
        load_artifact,
    )
    from .ai_workflow_declarations import load_route_declaration_locked
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        PROCESS_GENERATION,
        ROLES,
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        canonical_json,
        load_artifact,
    )
    from ai_workflow_declarations import load_route_declaration_locked


PREFLIGHT_RECORD_SCHEMA_VERSION = "ai-preflight-record-1"
LAUNCHER_VERSION = "ai-workflow-launcher-1"
RUNTIME_MANIFEST_FILENAME = "ai_workflow_runtime_files.json"
PREFLIGHT_LEDGER = "preflight-records.jsonl"
PINNED_ROLE_FIELDS = ("model", "reasoning_effort", "sandbox")
PREFLIGHT_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "role",
        "cache_key",
        "status",
        "route_config_hash",
        "runtime_profile_hash",
        "install_version",
        "launcher_version",
        "cwd",
        "worktree_id",
        "process_generation",
    }
)
REQUIRED_SCHEMA_FILES = frozenset(
    {
        "ai_workflow_task.schema.json",
        "ai_workflow_route_decision.schema.json",
        "ai_workflow_route_declaration.schema.json",
        "ai_workflow_preflight_record.schema.json",
    }
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


@dataclass(frozen=True)
class PreflightContext:
    task_id: str
    route_config_hash: str
    runtime_profile_hash: str
    install_version: str
    launcher_version: str
    cwd: str
    worktree_id: str
    process_generation: str

    def cache_key(self) -> str:
        payload = {
            "route_config_hash": self.route_config_hash,
            "runtime_profile_hash": self.runtime_profile_hash,
            "install_version": self.install_version,
            "launcher_version": self.launcher_version,
            "cwd": self.cwd,
            "worktree_id": self.worktree_id,
            "process_generation": self.process_generation,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_runtime_profile_hash(role_config: Mapping[str, object]) -> str:
    if not isinstance(role_config, Mapping):
        return ""
    values: dict[str, str] = {}
    for field in PINNED_ROLE_FIELDS:
        value = role_config.get(field)
        if not isinstance(value, str) or not value.strip():
            return ""
        values[field] = value
    values["permission"] = values["sandbox"]
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


def compute_install_version() -> str:
    path = _install_root() / "config" / RUNTIME_MANIFEST_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install runtime files manifest is missing or malformed",
        ) from exc
    if not isinstance(value, Mapping):
        _fail(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install runtime files manifest is missing or malformed",
        )
        raise AssertionError("unreachable")
    if value.get("schema_version") != "ai-runtime-files-1":
        _fail(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install runtime files manifest is missing or malformed",
        )
    digest = value.get("aggregate_sha256")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        _fail(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install runtime files manifest is missing or malformed",
        )
        raise AssertionError("unreachable")
    return digest


def _load_install_config() -> Mapping[str, object]:
    path = _install_root() / "config" / "ai_workflow.toml"
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowError(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install role configuration is missing or malformed",
        ) from exc
    if not isinstance(value, Mapping):
        _fail(
            "INSTALL_MANIFEST_UNAVAILABLE",
            "install role configuration is missing or malformed",
        )
        raise AssertionError("unreachable")
    return value


def _load_role_config(role: str) -> Mapping[str, object]:
    config = _load_install_config()
    roles = config.get("roles")
    if not isinstance(roles, Mapping):
        return {}
    spec = roles.get(role)
    if not isinstance(spec, Mapping):
        return {}
    return spec


def _git_toplevel(repo: Path) -> str:
    try:
        cwd = Path(repo).resolve()
    except OSError as exc:
        raise WorkflowError(
            "WORKTREE_UNAVAILABLE", "task envelope repository cannot be resolved"
        ) from exc
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    toplevel = result.stdout.strip()
    if result.returncode != 0 or not toplevel:
        _fail("WORKTREE_UNAVAILABLE", "task envelope repository is not a git worktree")
        raise AssertionError("unreachable")
    return str(Path(toplevel).resolve())


def _envelope_repo(task: Mapping[str, object]) -> Path:
    if task.get("task_type") == "REMEDIATION":
        raw = task.get("source_worktree")
    else:
        raw = task.get("repository_root")
    if not isinstance(raw, str) or not raw.strip():
        _fail("WORKTREE_UNAVAILABLE", "task envelope repository path is missing")
        raise AssertionError("unreachable")
    return Path(raw)


def _schema_files_present() -> bool:
    config_dir = _install_root() / "config"
    return all((config_dir / name).is_file() for name in REQUIRED_SCHEMA_FILES)


def compute_preflight_context(
    store: TaskStoreProtocol, task_id: str, *, role: str
) -> PreflightContext:
    store._assert_lock_held(task_id)
    declaration = load_route_declaration_locked(store, task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
        raise AssertionError("unreachable")
    try:
        task = load_artifact(store._require_task(task_id) / "task.json")
    except ArtifactError as exc:
        raise WorkflowError(
            "WORKTREE_UNAVAILABLE", "cannot read stored task envelope"
        ) from exc
    return PreflightContext(
        task_id=task_id,
        route_config_hash=_string(declaration.route_config_hash, "route_config_hash"),
        runtime_profile_hash=compute_runtime_profile_hash(_load_role_config(role)),
        install_version=compute_install_version(),
        launcher_version=LAUNCHER_VERSION,
        cwd=os.getcwd(),
        worktree_id=_git_toplevel(_envelope_repo(task)),
        process_generation=PROCESS_GENERATION,
    )


def _run_preflight_checks(role: str, context: PreflightContext) -> Mapping[str, object]:
    if role not in ROLES:
        _fail("INVALID_ENUM", "role is not supported")
    status = "PASS"
    if not _HEX64.fullmatch(context.runtime_profile_hash or ""):
        status = "FAIL"
    if not _HEX64.fullmatch(context.install_version or ""):
        status = "FAIL"
    if not _HEX64.fullmatch(context.route_config_hash or ""):
        status = "FAIL"
    for value in (
        context.task_id,
        context.launcher_version,
        context.cwd,
        context.worktree_id,
        context.process_generation,
    ):
        if not isinstance(value, str) or not value.strip():
            status = "FAIL"
    return {
        "schema_version": PREFLIGHT_RECORD_SCHEMA_VERSION,
        "task_id": context.task_id,
        "role": role,
        "cache_key": context.cache_key(),
        "status": status,
    }


def _preflight_record_matches(
    records: tuple[dict[str, object], ...], role: str, cache_key: str
) -> bool:
    matched = False
    for record in records:
        if record.get("role") == role and record.get("cache_key") == cache_key:
            matched = record.get("status") == "PASS"
    return matched


def _read_preflight_records(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        rows = store.read_task_ledger(task_id, PREFLIGHT_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("PREFLIGHT_LEDGER_CORRUPT", exc.message)
        raise
    for row in rows:
        if row.get("task_id") != task_id:
            _fail("PREFLIGHT_LEDGER_CORRUPT", "preflight record task_id does not match")
    return rows


def _append_preflight_record(
    store: TaskStoreProtocol,
    task_id: str,
    role: str,
    context: PreflightContext,
    result: Mapping[str, object],
) -> dict[str, object]:
    record = {
        "schema_version": PREFLIGHT_RECORD_SCHEMA_VERSION,
        "task_id": task_id,
        "role": role,
        "cache_key": result["cache_key"],
        "status": result["status"],
        "route_config_hash": context.route_config_hash,
        "runtime_profile_hash": context.runtime_profile_hash,
        "install_version": context.install_version,
        "launcher_version": context.launcher_version,
        "cwd": context.cwd,
        "worktree_id": context.worktree_id,
        "process_generation": context.process_generation,
    }
    extra = set(record) - PREFLIGHT_RECORD_FIELDS
    if extra:
        _fail("UNKNOWN_FIELD", f"unsupported field {sorted(extra)[0]}")
    store.append_task_ledger(task_id, PREFLIGHT_LEDGER, record)
    return record


def run_role_preflight_locked(
    store: TaskStoreProtocol, task_id: str, role: str
) -> Mapping[str, object]:
    store._assert_lock_held(task_id)
    context = compute_preflight_context(store, task_id, role=role)
    result = dict(_run_preflight_checks(role, context))
    if not _schema_files_present():
        result["status"] = "FAIL"
    return _append_preflight_record(store, task_id, role, context, result)


def run_role_preflight(
    store: TaskStoreProtocol, task_id: str, role: str
) -> Mapping[str, object]:
    with store.lock(task_id):
        return run_role_preflight_locked(store, task_id, role)


def is_role_preflighted_locked(
    store: TaskStoreProtocol, task_id: str, role: str
) -> bool:
    store._assert_lock_held(task_id)
    context = compute_preflight_context(store, task_id, role=role)
    records = _read_preflight_records(store, task_id)
    return _preflight_record_matches(records, role, context.cache_key())


def is_role_preflighted(store: TaskStoreProtocol, task_id: str, role: str) -> bool:
    with store.lock(task_id):
        return is_role_preflighted_locked(store, task_id, role)


def require_role_preflighted_locked(
    store: TaskStoreProtocol, task_id: str, role: str
) -> None:
    store._assert_lock_held(task_id)
    context = compute_preflight_context(store, task_id, role=role)
    records = _read_preflight_records(store, task_id)
    if not _preflight_record_matches(records, role, context.cache_key()):
        _fail("ROLE_NOT_PREFLIGHTED", f"role {role} is not preflighted")


def require_role_preflighted(
    store: TaskStoreProtocol, task_id: str, role: str
) -> None:
    with store.lock(task_id):
        require_role_preflighted_locked(store, task_id, role)
