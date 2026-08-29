"""Envelope-derived candidate tree/diff digests with control-plane exclusions."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from .ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
    )
    from .ai_workflow_planning import normalize_scope
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
    )
    from ai_workflow_planning import normalize_scope


CANDIDATE_STATE_SCHEMA_VERSION = "ai-candidate-state-1"
CANDIDATE_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "envelope_hash",
        "candidate_commit",
        "baseline_commit",
        "tree_digest",
        "diff_digest",
        "runtime_evidence_ids",
        "captured_at_utc",
    }
)
ENTRY_KINDS = frozenset({"file", "link"})
RUNTIME_SESSIONS_PREFIX = PurePosixPath(".codex/sessions")
GIT_DIR_PREFIX = PurePosixPath(".git")
STATE_ROOT_PREFIX = PurePosixPath("data/state/ai-workflow")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_DIFF_GIT = re.compile(br"^diff --git ", re.M)


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _nfc_posix(path: str) -> str:
    return unicodedata.normalize("NFC", path.replace("\\", "/"))


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _hex_digest(value: object, field: str, pattern: re.Pattern[str]) -> str:
    text = _string(value, field)
    if not pattern.fullmatch(text):
        _fail("INVALID_TYPE", f"{field} must be a lowercase hexadecimal digest")
    return text


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
    )


def _git_text(repo: Path, args: list[str], *, code: str) -> str:
    result = _run_git(repo, args)
    if result.returncode != 0:
        _fail(code, f"git {' '.join(args)} failed")
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _require_git_worktree(repo: Path) -> Path:
    try:
        resolved = Path(repo).resolve()
    except OSError:
        _fail("CANDIDATE_REPO_INVALID", "candidate root cannot be resolved")
        raise AssertionError("unreachable")
    if not resolved.is_dir():
        _fail("CANDIDATE_REPO_INVALID", "candidate root is not a directory")
    inside = _run_git(resolved, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        _fail("CANDIDATE_REPO_INVALID", "candidate root is not a git worktree")
    toplevel = _run_git(resolved, ["rev-parse", "--show-toplevel"])
    if toplevel.returncode != 0:
        _fail("CANDIDATE_REPO_INVALID", "candidate root is not a git worktree")
    root = Path(toplevel.stdout.decode("utf-8", "surrogateescape").strip()).resolve()
    if root != resolved:
        _fail("CANDIDATE_REPO_INVALID", "candidate root must be the git worktree root")
    return resolved


def _repo_relative_prefix(repo: Path, path: Path) -> PurePosixPath:
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(repo)
    except (OSError, ValueError):
        _fail("CANDIDATE_REPO_INVALID", "exclusion prefix is outside the repository")
        raise AssertionError("unreachable")
    posix = _nfc_posix(relative.as_posix())
    if posix in {"", "."}:
        _fail("CANDIDATE_REPO_INVALID", "exclusion prefix is outside the repository")
    try:
        return normalize_scope(posix)
    except Exception as exc:
        if getattr(exc, "code", None) == "PLAN_INVALID":
            _fail(
                "CANDIDATE_REPO_INVALID",
                "exclusion prefix is not a repository-relative path",
            )
        raise


def _is_excluded(path: str, exclusions: tuple[PurePosixPath, ...]) -> bool:
    posix = PurePosixPath(_nfc_posix(path))
    for prefix in exclusions:
        if posix == prefix or prefix in posix.parents:
            return True
    return False


@dataclass(frozen=True)
class CandidateEntry:
    path: str
    mode: str
    kind: str
    content_sha256: str


@dataclass(frozen=True)
class CandidateState:
    schema_version: str
    task_id: str
    envelope_hash: str
    candidate_commit: str
    baseline_commit: str
    tree_digest: str
    diff_digest: str
    runtime_evidence_ids: tuple[str, ...]
    captured_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "envelope_hash": self.envelope_hash,
            "candidate_commit": self.candidate_commit,
            "baseline_commit": self.baseline_commit,
            "tree_digest": self.tree_digest,
            "diff_digest": self.diff_digest,
            "runtime_evidence_ids": list(self.runtime_evidence_ids),
            "captured_at_utc": self.captured_at_utc,
        }

    def state_digest(self) -> str:
        payload = {
            "baseline_commit": self.baseline_commit,
            "candidate_commit": self.candidate_commit,
            "diff_digest": self.diff_digest,
            "runtime_evidence_ids": sorted(set(self.runtime_evidence_ids)),
            "tree_digest": self.tree_digest,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_candidate_state(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "candidate state must be an object")
    payload = dict(value)
    unknown = sorted(set(payload) - CANDIDATE_STATE_FIELDS)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(CANDIDATE_STATE_FIELDS - set(payload))
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")
    if payload.get("schema_version") != CANDIDATE_STATE_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION", f"schema_version must be {CANDIDATE_STATE_SCHEMA_VERSION}")
    _string(payload["task_id"], "task_id")
    _hex_digest(payload["envelope_hash"], "envelope_hash", _HEX64)
    _hex_digest(payload["candidate_commit"], "candidate_commit", _HEX40)
    _hex_digest(payload["baseline_commit"], "baseline_commit", _HEX40)
    _hex_digest(payload["tree_digest"], "tree_digest", _HEX64)
    _hex_digest(payload["diff_digest"], "diff_digest", _HEX64)
    _string(payload["captured_at_utc"], "captured_at_utc")
    ids = payload["runtime_evidence_ids"]
    if not isinstance(ids, list):
        _fail("INVALID_TYPE", "runtime_evidence_ids must be an array")
    for index, item in enumerate(ids):
        _string(item, f"runtime_evidence_ids[{index}]")


def candidate_exclusions(repo: Path, state_root: Path) -> tuple[PurePosixPath, ...]:
    resolved_repo = Path(repo).resolve()
    if not resolved_repo.is_dir():
        _fail("CANDIDATE_REPO_INVALID", "candidate root is not a directory")
    state_prefix = _repo_relative_prefix(resolved_repo, Path(state_root))
    sessions_prefix = _repo_relative_prefix(
        resolved_repo, resolved_repo / RUNTIME_SESSIONS_PREFIX
    )
    return (GIT_DIR_PREFIX, state_prefix, sessions_prefix)


def candidate_root_from_envelope(task: Mapping[str, object]) -> Path:
    if not isinstance(task, Mapping):
        _fail("CANDIDATE_REPO_INVALID", "task envelope must be an object")
    if task.get("task_type") == "REMEDIATION":
        raw = task.get("source_worktree")
    else:
        raw = task.get("repository_root")
    if not isinstance(raw, str) or not raw.strip():
        _fail("CANDIDATE_REPO_INVALID", "candidate root is missing from the task envelope")
    return _require_git_worktree(Path(raw))


def _decode_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _ls_tracked(repo: Path) -> tuple[tuple[str, str], ...]:
    result = _run_git(repo, ["ls-files", "--stage", "-z"])
    if result.returncode != 0:
        _fail("CANDIDATE_REPO_INVALID", "cannot list tracked candidate files")
    entries: list[tuple[str, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            meta, path_raw = record.split(b"\t", 1)
            mode, _sha, _stage = meta.split(b" ")
        except ValueError:
            _fail("CANDIDATE_REPO_INVALID", "cannot parse tracked candidate files")
            raise AssertionError("unreachable")
        path = _nfc_posix(_decode_git_path(path_raw))
        entries.append((mode.decode("ascii"), path))
    return tuple(entries)


def _ls_untracked(repo: Path) -> tuple[str, ...]:
    result = _run_git(repo, ["ls-files", "-z", "--others", "--exclude-standard"])
    if result.returncode != 0:
        _fail("CANDIDATE_REPO_INVALID", "cannot list untracked candidate files")
    paths = []
    for raw in result.stdout.split(b"\0"):
        if raw:
            paths.append(_nfc_posix(_decode_git_path(raw)))
    return tuple(paths)


def _entry_from_worktree(repo: Path, relative: str) -> CandidateEntry | None:
    path = repo / relative
    try:
        metadata = path.lstat()
    except OSError:
        return None
    nfc_path = _nfc_posix(relative)
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path)
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return CandidateEntry(
            path=nfc_path,
            mode="120000",
            kind="link",
            content_sha256=digest,
        )
    if stat.S_ISDIR(metadata.st_mode):
        if (path / ".git").exists():
            _fail("CANDIDATE_DIGEST_UNSUPPORTED", "submodule candidate path is unsupported")
        return None
    if not stat.S_ISREG(metadata.st_mode):
        _fail("CANDIDATE_DIGEST_UNSUPPORTED", "candidate path type is unsupported")
    mode = "100755" if metadata.st_mode & 0o111 else "100644"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return CandidateEntry(
        path=nfc_path,
        mode=mode,
        kind="file",
        content_sha256=digest,
    )


def scan_candidate_manifest(
    repo: Path, *, exclusions: tuple[PurePosixPath, ...]
) -> tuple[CandidateEntry, ...]:
    resolved = _require_git_worktree(repo)
    collected: dict[str, CandidateEntry] = {}
    for mode, relative in _ls_tracked(resolved):
        if mode == "160000":
            _fail("CANDIDATE_DIGEST_UNSUPPORTED", "submodule candidate path is unsupported")
        if _is_excluded(relative, exclusions):
            continue
        entry = _entry_from_worktree(resolved, relative)
        if entry is None:
            continue
        collected[entry.path] = entry
    for relative in _ls_untracked(resolved):
        if _is_excluded(relative, exclusions):
            continue
        entry = _entry_from_worktree(resolved, relative)
        if entry is None:
            continue
        collected[entry.path] = entry
    return tuple(collected[key] for key in sorted(collected))


def compute_tree_digest(manifest: tuple[CandidateEntry, ...]) -> str:
    lines = [
        f"{entry.mode} {entry.kind} {entry.path} {entry.content_sha256}"
        for entry in sorted(manifest, key=lambda item: item.path)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _unquote_diff_path(value: str) -> str:
    text = value
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
        text = bytes(text, "utf-8").decode("unicode_escape")
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    if text == "/dev/null":
        return text
    return _nfc_posix(text)


def _hunk_paths(chunk: bytes) -> tuple[str, ...]:
    header = chunk.split(b"\n", 1)[0].decode("utf-8", "surrogateescape")
    if not header.startswith("diff --git "):
        return ()
    payload = header[len("diff --git "):]
    if payload.startswith('"'):
        parts = re.findall(r'"([^"]*)"', payload)
        if len(parts) < 2:
            return ()
        left, right = parts[0], parts[1]
    else:
        marker = " b/"
        index = payload.find(marker)
        if index < 0:
            return ()
        left, right = payload[:index], payload[index + 1:]
    return tuple(path for path in (_unquote_diff_path(left), _unquote_diff_path(right)) if path)


def _drop_excluded_diff_hunks(
    blob: bytes, exclusions: tuple[PurePosixPath, ...]
) -> bytes:
    if not blob:
        return b""
    starts = [match.start() for match in _DIFF_GIT.finditer(blob)]
    if not starts:
        return blob
    kept: list[bytes] = []
    if starts[0] > 0:
        kept.append(blob[: starts[0]])
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(blob)
        chunk = blob[start:end]
        paths = _hunk_paths(chunk)
        if any(_is_excluded(path, exclusions) for path in paths if path != "/dev/null"):
            continue
        kept.append(chunk)
    return b"".join(kept)


def _git_diff_bytes(
    repo: Path, baseline_commit: str, pathspecs: list[str]
) -> bytes:
    result = _run_git(
        repo,
        ["diff", "--binary", "--full-index", baseline_commit, "--", ".", *pathspecs],
    )
    if result.returncode not in (0, 1):
        _fail("CANDIDATE_REPO_INVALID", "cannot compute candidate diff")
    return result.stdout


def compute_diff_digest(
    repo: Path,
    *,
    baseline_commit: str,
    exclusions: tuple[PurePosixPath, ...],
    untracked: tuple[CandidateEntry, ...],
) -> str:
    pathspecs = [f":(exclude){prefix.as_posix()}/**" for prefix in exclusions]
    blob = _git_diff_bytes(repo, baseline_commit, pathspecs)
    filtered = _drop_excluded_diff_hunks(blob, exclusions)
    normalized_untracked = "\n".join(
        f"{entry.path} {entry.content_sha256}"
        for entry in sorted(untracked, key=lambda item: item.path)
    )
    material = filtered + normalized_untracked.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _untracked_from_manifest(
    repo: Path, manifest: tuple[CandidateEntry, ...]
) -> tuple[CandidateEntry, ...]:
    names = set(_ls_untracked(repo))
    return tuple(entry for entry in manifest if entry.path in names)


def _assert_baseline_ancestor(repo: Path, baseline_commit: str) -> None:
    if not isinstance(baseline_commit, str) or not _HEX40.fullmatch(baseline_commit):
        _fail("CANDIDATE_BASELINE_INVALID", "baseline_commit must be a 40-character commit")
    result = _run_git(repo, ["merge-base", "--is-ancestor", baseline_commit, "HEAD"])
    if result.returncode != 0:
        _fail("CANDIDATE_BASELINE_INVALID", "baseline_commit is not an ancestor of HEAD")


def _rev_parse_head(repo: Path) -> str:
    value = _git_text(repo, ["rev-parse", "HEAD"], code="CANDIDATE_REPO_INVALID")
    if not _HEX40.fullmatch(value):
        _fail("CANDIDATE_REPO_INVALID", "HEAD is not a 40-character commit")
    return value


def _evidence_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        _fail("INVALID_TYPE", "runtime_evidence_ids must be a tuple")
    ids: list[str] = []
    for index, item in enumerate(values):
        ids.append(_string(item, f"runtime_evidence_ids[{index}]"))
    return tuple(ids)


def capture_candidate_state(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    baseline_commit: str,
    runtime_evidence_ids: tuple[str, ...],
) -> CandidateState:
    evidence_ids = _evidence_ids(runtime_evidence_ids)
    task_dir = store._require_task(task_id)
    task = load_artifact(task_dir / "task.json")
    repo = candidate_root_from_envelope(task)
    exclusions = candidate_exclusions(repo, repo / STATE_ROOT_PREFIX)
    _assert_baseline_ancestor(repo, baseline_commit)
    head1 = _rev_parse_head(repo)
    manifest1 = scan_candidate_manifest(repo, exclusions=exclusions)
    tree_digest = compute_tree_digest(manifest1)
    diff_digest = compute_diff_digest(
        repo,
        baseline_commit=baseline_commit,
        exclusions=exclusions,
        untracked=_untracked_from_manifest(repo, manifest1),
    )
    head2 = _rev_parse_head(repo)
    manifest2 = scan_candidate_manifest(repo, exclusions=exclusions)
    if head1 != head2 or manifest1 != manifest2:
        _fail("CANDIDATE_STATE_UNSTABLE", "candidate HEAD or manifest changed during capture")
    envelope_hash = artifact_sha256(task)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = CandidateState(
        schema_version=CANDIDATE_STATE_SCHEMA_VERSION,
        task_id=_string(task.get("task_id"), "task_id"),
        envelope_hash=envelope_hash,
        candidate_commit=head1,
        baseline_commit=baseline_commit,
        tree_digest=tree_digest,
        diff_digest=diff_digest,
        runtime_evidence_ids=evidence_ids,
        captured_at_utc=captured_at,
    )
    validate_candidate_state(state.to_dict())
    return state
