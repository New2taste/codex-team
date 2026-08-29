"""CandidateState digest: envelope root, pathspec exclusions, manifest double-scan."""

from __future__ import annotations

import inspect
import stat
import subprocess
import tempfile
import unittest
import unicodedata
from pathlib import Path, PurePosixPath
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_candidate_state as candidate_state


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace')}"
        )
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.email", "candidate@example.test")
    _run_git(path, "config", "user.name", "Candidate Test")
    _run_git(path, "config", "commit.gpgsign", "false")
    _run_git(path, "config", "core.autocrlf", "false")
    return path


def _write(repo: Path, relative: str, content: str) -> Path:
    destination = repo / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _commit(repo: Path, message: str) -> str:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", message)
    return _head(repo)


def _head(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()


def _valid_task(
    repository_root: Path,
    *,
    task_id: str = TASK_ID,
    task_type: str = "PLAN",
    source_worktree: str | None = None,
) -> dict[str, object]:
    task: dict[str, object] = {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": task_type,
        "objective": "Capture a candidate digest for the frozen envelope",
        "repository_root": str(repository_root),
        "source_worktree": source_worktree,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["src/app.py"] if task_type == "REMEDIATION" else [],
        "forbidden_actions": ["merge", "push", "change_constitution"],
        "risk_flags": [],
        "acceptance_commands": [],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL"],
    }
    return task


def _exclusions(repo: Path) -> tuple[PurePosixPath, ...]:
    return candidate_state.candidate_exclusions(
        repo, repo / "data" / "state" / "ai-workflow"
    )


def _untracked_entries(
    repo: Path, manifest: tuple[candidate_state.CandidateEntry, ...]
) -> tuple[candidate_state.CandidateEntry, ...]:
    listed = _run_git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    names = {
        unicodedata.normalize("NFC", item.replace("\\", "/"))
        for item in listed.stdout.decode("utf-8").split("\0")
        if item
    }
    return tuple(entry for entry in manifest if entry.path in names)


def _tree_and_diff(repo: Path, baseline: str) -> tuple[str, str]:
    exclusions = _exclusions(repo)
    manifest = candidate_state.scan_candidate_manifest(repo, exclusions=exclusions)
    untracked = _untracked_entries(repo, manifest)
    return (
        candidate_state.compute_tree_digest(manifest),
        candidate_state.compute_diff_digest(
            repo,
            baseline_commit=baseline,
            exclusions=exclusions,
            untracked=untracked,
        ),
    )


def _capture(
    store: workflow.WorkflowStore,
    task_id: str,
    *,
    baseline_commit: str,
    runtime_evidence_ids: tuple[str, ...] = (),
) -> candidate_state.CandidateState:
    return candidate_state.capture_candidate_state(
        store,
        task_id,
        baseline_commit=baseline_commit,
        runtime_evidence_ids=runtime_evidence_ids,
    )


def _store_for_repo(repo: Path, task: dict[str, object]) -> workflow.WorkflowStore:
    store_root = repo / "data" / "state" / "ai-workflow"
    task_dir = store_root / str(task["task_id"])
    task_dir.mkdir(parents=True, exist_ok=True)
    store = workflow.WorkflowStore(store_root)
    task_path = task_dir / "task.json"
    if task_path.exists():
        task_path.write_text(
            artifacts.canonical_json(task) + "\n",
            encoding="utf-8",
        )
        return store
    try:
        store.create_task(task)
    except artifacts.WorkflowError as exc:
        if exc.code != "TASK_EXISTS":
            raise
        task_path.write_text(
            artifacts.canonical_json(task) + "\n",
            encoding="utf-8",
        )
    return store


class DigestSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self.temporary.name) / "repo")
        _write(self.repo, "README.md", "hello\n")
        _write(self.repo, "src/app.py", "print(1)\n")
        self.baseline = _commit(self.repo, "baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_identical_trees_in_two_directories_share_tree_digest(self) -> None:
        other = _init_repo(Path(self.temporary.name) / "twin")
        _write(other, "README.md", "hello\n")
        _write(other, "src/app.py", "print(1)\n")
        _commit(other, "baseline")
        left, _ = _tree_and_diff(self.repo, self.baseline)
        right, _ = _tree_and_diff(other, _head(other))
        self.assertEqual(left, right)
        self.assertEqual(left, _tree_and_diff(self.repo, self.baseline)[0])

    def test_tracked_content_change_flips_tree_and_diff_digest(self) -> None:
        before_tree, before_diff = _tree_and_diff(self.repo, self.baseline)
        _write(self.repo, "src/app.py", "print(2)\n")
        after_tree, after_diff = _tree_and_diff(self.repo, self.baseline)
        self.assertNotEqual(before_tree, after_tree)
        self.assertNotEqual(before_diff, after_diff)

    def test_untracked_file_alone_flips_tree_and_diff_digest(self) -> None:
        before_tree, before_diff = _tree_and_diff(self.repo, self.baseline)
        _write(self.repo, "notes.txt", "scratch\n")
        after_tree, after_diff = _tree_and_diff(self.repo, self.baseline)
        self.assertNotEqual(before_tree, after_tree)
        self.assertNotEqual(before_diff, after_diff)

    def test_posix_mode_change_flips_tree_digest(self) -> None:
        before_tree, _ = _tree_and_diff(self.repo, self.baseline)
        target = self.repo / "src" / "app.py"
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        after_tree, _ = _tree_and_diff(self.repo, self.baseline)
        self.assertNotEqual(before_tree, after_tree)
        modes = {
            entry.mode
            for entry in candidate_state.scan_candidate_manifest(
                self.repo, exclusions=_exclusions(self.repo)
            )
            if entry.path == "src/app.py"
        }
        self.assertEqual({"100755"}, modes)

    def test_file_deletion_flips_tree_and_diff_digest(self) -> None:
        before_tree, before_diff = _tree_and_diff(self.repo, self.baseline)
        (self.repo / "src" / "app.py").unlink()
        after_tree, after_diff = _tree_and_diff(self.repo, self.baseline)
        self.assertNotEqual(before_tree, after_tree)
        self.assertNotEqual(before_diff, after_diff)

    def test_symlink_target_change_flips_tree_digest(self) -> None:
        link = self.repo / "alias"
        link.symlink_to("README.md")
        _run_git(self.repo, "add", "alias")
        _run_git(self.repo, "commit", "-m", "add symlink")
        before_tree, _ = _tree_and_diff(self.repo, _head(self.repo))
        link.unlink()
        link.symlink_to("src/app.py")
        after_tree, _ = _tree_and_diff(self.repo, _head(self.repo))
        self.assertNotEqual(before_tree, after_tree)
        kinds = {
            entry.kind
            for entry in candidate_state.scan_candidate_manifest(
                self.repo, exclusions=_exclusions(self.repo)
            )
            if entry.path == "alias"
        }
        self.assertEqual({"link"}, kinds)

    def test_path_case_is_not_folded(self) -> None:
        mixed = _write(self.repo, "ReadMe.TXT", "cased\n")
        _run_git(self.repo, "add", str(mixed.relative_to(self.repo)))
        _run_git(self.repo, "commit", "-m", "mixed case")
        paths = [
            entry.path
            for entry in candidate_state.scan_candidate_manifest(
                self.repo, exclusions=_exclusions(self.repo)
            )
        ]
        self.assertIn("ReadMe.TXT", paths)
        self.assertNotIn("readme.txt", paths)
        left = candidate_state.CandidateEntry(
            path="Foo",
            mode="100644",
            kind="file",
            content_sha256="a" * 64,
        )
        right = candidate_state.CandidateEntry(
            path="foo",
            mode="100644",
            kind="file",
            content_sha256="a" * 64,
        )
        self.assertNotEqual(
            candidate_state.compute_tree_digest((left,)),
            candidate_state.compute_tree_digest((right,)),
        )

    def test_submodule_gitlink_is_unsupported(self) -> None:
        gitlink = self.repo / "vendor" / "dep"
        gitlink.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.baseline},vendor/dep",
        )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "CANDIDATE_DIGEST_UNSUPPORTED"
        ):
            candidate_state.scan_candidate_manifest(
                self.repo, exclusions=_exclusions(self.repo)
            )


class ControlPlaneExclusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self.temporary.name) / "repo")
        _write(self.repo, "README.md", "hello\n")
        self.events = _write(
            self.repo,
            f"data/state/ai-workflow/{TASK_ID}/events.jsonl",
            '{"event_type":"CREATED"}\n',
        )
        self.baseline = _commit(self.repo, "baseline with control plane")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _digests(self) -> tuple[str, str]:
        return _tree_and_diff(self.repo, self.baseline)

    def test_tracked_control_plane_mutation_does_not_change_digests(self) -> None:
        before_tree, before_diff = self._digests()
        self.events.write_text('{"event_type":"MUTATED"}\n', encoding="utf-8")
        after_tree, after_diff = self._digests()
        self.assertEqual(before_tree, after_tree)
        self.assertEqual(before_diff, after_diff)
        paths = [
            entry.path
            for entry in candidate_state.scan_candidate_manifest(
                self.repo, exclusions=_exclusions(self.repo)
            )
        ]
        self.assertNotIn(
            f"data/state/ai-workflow/{TASK_ID}/events.jsonl",
            paths,
        )

    def test_pathspec_defense_still_excludes_when_hunk_filter_is_removed(self) -> None:
        before = candidate_state.compute_diff_digest(
            self.repo,
            baseline_commit=self.baseline,
            exclusions=_exclusions(self.repo),
            untracked=(),
        )
        self.events.write_text('{"event_type":"MUTATED"}\n', encoding="utf-8")
        with mock.patch.object(
            candidate_state,
            "_drop_excluded_diff_hunks",
            lambda blob, exclusions: blob,
        ):
            after = candidate_state.compute_diff_digest(
                self.repo,
                baseline_commit=self.baseline,
                exclusions=_exclusions(self.repo),
                untracked=(),
            )
        self.assertEqual(before, after)

    def test_hunk_filter_defense_still_excludes_when_pathspec_is_removed(self) -> None:
        before = candidate_state.compute_diff_digest(
            self.repo,
            baseline_commit=self.baseline,
            exclusions=_exclusions(self.repo),
            untracked=(),
        )
        self.events.write_text('{"event_type":"MUTATED"}\n', encoding="utf-8")
        real_run = candidate_state.subprocess.run

        def stripped(argv, *args, **kwargs):
            argv = [
                item
                for item in argv
                if not (isinstance(item, str) and item.startswith(":(exclude)"))
            ]
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(candidate_state.subprocess, "run", stripped):
            after = candidate_state.compute_diff_digest(
                self.repo,
                baseline_commit=self.baseline,
                exclusions=_exclusions(self.repo),
                untracked=(),
            )
        self.assertEqual(before, after)

    def test_diff_digest_wires_both_pathspec_and_hunk_filter(self) -> None:
        source = inspect.getsource(candidate_state.compute_diff_digest)
        self.assertIn(":(exclude)", source)
        self.assertIn("_drop_excluded_diff_hunks", source)

    def test_non_git_directory_is_invalid_repo(self) -> None:
        plain = Path(self.temporary.name) / "not-git"
        plain.mkdir()
        task = _valid_task(plain)
        with self.assertRaisesRegex(artifacts.WorkflowError, "CANDIDATE_REPO_INVALID"):
            candidate_state.candidate_root_from_envelope(task)

    def test_state_root_outside_repo_is_invalid(self) -> None:
        outside = Path(self.temporary.name) / "outside-state"
        outside.mkdir()
        with self.assertRaisesRegex(artifacts.WorkflowError, "CANDIDATE_REPO_INVALID"):
            candidate_state.candidate_exclusions(self.repo, outside)

    def test_baseline_that_is_not_an_ancestor_is_invalid(self) -> None:
        _run_git(self.repo, "checkout", "--orphan", "other")
        _write(self.repo, "orphan.txt", "side\n")
        other = _commit(self.repo, "orphan")
        _run_git(self.repo, "checkout", "main")
        store = _store_for_repo(self.repo, _valid_task(self.repo))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "CANDIDATE_BASELINE_INVALID"
        ):
            _capture(store, TASK_ID, baseline_commit=other)

    def test_branch_name_baseline_is_invalid(self) -> None:
        store = _store_for_repo(self.repo, _valid_task(self.repo))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "CANDIDATE_BASELINE_INVALID"
        ):
            _capture(store, TASK_ID, baseline_commit="main")


class QuasiAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self.temporary.name) / "repo")
        self.app = _write(self.repo, "src/app.py", "v1\n")
        _write(self.repo, "README.md", "hello\n")
        self.baseline = _commit(self.repo, "baseline")
        self.app.write_text("v2\n", encoding="utf-8")
        status = _run_git(self.repo, "status", "--porcelain", "--", "src/app.py")
        self.assertEqual(b"M", status.stdout[1:2])
        self.store = workflow.WorkflowStore(self.repo / "data" / "state" / "ai-workflow")
        self.store.create_task(_valid_task(self.repo))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_content_change_with_stable_porcelain_letter_is_unstable(self) -> None:
        original = candidate_state.scan_candidate_manifest
        calls = {"n": 0}

        def wrapped(repo: Path, *, exclusions: tuple[PurePosixPath, ...]):
            calls["n"] += 1
            if calls["n"] == 2:
                self.app.write_text("v3\n", encoding="utf-8")
                status = _run_git(self.repo, "status", "--porcelain", "--", "src/app.py")
                self.assertEqual(b"M", status.stdout[1:2])
            return original(repo, exclusions=exclusions)

        with mock.patch.object(candidate_state, "scan_candidate_manifest", wrapped):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "CANDIDATE_STATE_UNSTABLE"
            ):
                _capture(self.store, TASK_ID, baseline_commit=self.baseline)
        self.assertGreaterEqual(calls["n"], 2)

    def test_head_advance_between_scans_is_unstable(self) -> None:
        original = candidate_state.scan_candidate_manifest
        calls = {"n": 0}

        def wrapped(repo: Path, *, exclusions: tuple[PurePosixPath, ...]):
            calls["n"] += 1
            result = original(repo, exclusions=exclusions)
            if calls["n"] == 1:
                _run_git(self.repo, "commit", "--allow-empty", "-m", "advance")
            return result

        with mock.patch.object(candidate_state, "scan_candidate_manifest", wrapped):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "CANDIDATE_STATE_UNSTABLE"
            ):
                _capture(self.store, TASK_ID, baseline_commit=self.baseline)
        self.assertGreaterEqual(calls["n"], 1)

    def test_stable_double_scan_returns_candidate_state(self) -> None:
        state = _capture(self.store, TASK_ID, baseline_commit=self.baseline)
        candidate_state.validate_candidate_state(state.to_dict())
        self.assertEqual(self.baseline, state.baseline_commit)
        self.assertEqual(_head(self.repo), state.candidate_commit)
        self.assertEqual("ai-candidate-state-1", state.schema_version)


class EnvelopeRootAndRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.host = _init_repo(Path(self.temporary.name) / "host")
        self.worktree = _init_repo(Path(self.temporary.name) / "worktree")
        _write(self.host, "README.md", "host\n")
        _write(self.host, "marker-host.txt", "host-only\n")
        self.host_baseline = _commit(self.host, "host baseline")
        _write(self.worktree, "README.md", "worktree\n")
        _write(self.worktree, "marker-worktree.txt", "worktree-only\n")
        self.worktree_baseline = _commit(self.worktree, "worktree baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _capture_observing_root(
        self,
        store: workflow.WorkflowStore,
        task_id: str,
        *,
        baseline_commit: str,
    ) -> tuple[candidate_state.CandidateState, list[Path]]:
        observed: list[Path] = []
        original = candidate_state.scan_candidate_manifest

        def wrapped(repo: Path, *, exclusions: tuple[PurePosixPath, ...]):
            observed.append(Path(repo).resolve())
            return original(repo, exclusions=exclusions)

        with mock.patch.object(candidate_state, "scan_candidate_manifest", wrapped):
            state = _capture(store, task_id, baseline_commit=baseline_commit)
        return state, observed

    def test_plan_capture_uses_repository_root_not_source_worktree(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(
            _valid_task(self.host, source_worktree=str(self.worktree), task_type="PLAN")
        )
        state, observed = self._capture_observing_root(
            store, TASK_ID, baseline_commit=self.host_baseline
        )
        self.assertTrue(observed)
        self.assertEqual({self.host.resolve()}, set(observed))
        self.assertEqual(self.host_baseline, state.candidate_commit)
        paths = [
            entry.path
            for entry in candidate_state.scan_candidate_manifest(
                self.host, exclusions=_exclusions(self.host)
            )
        ]
        self.assertIn("marker-host.txt", paths)
        self.assertNotIn("marker-worktree.txt", paths)

    def test_remediation_capture_uses_source_worktree(self) -> None:
        store = workflow.WorkflowStore(
            self.worktree / "data" / "state" / "ai-workflow"
        )
        store.create_task(
            _valid_task(
                self.host,
                source_worktree=str(self.worktree),
                task_type="REMEDIATION",
            )
        )
        state, observed = self._capture_observing_root(
            store, TASK_ID, baseline_commit=self.worktree_baseline
        )
        self.assertTrue(observed)
        self.assertEqual({self.worktree.resolve()}, set(observed))
        self.assertEqual(self.worktree_baseline, state.candidate_commit)
        root = candidate_state.candidate_root_from_envelope(
            artifacts.load_artifact(
                store._require_task(TASK_ID) / "task.json"
            )
        )
        self.assertEqual(self.worktree.resolve(), root.resolve())

    def test_capture_signature_has_no_repo_or_root_parameter(self) -> None:
        parameters = inspect.signature(
            candidate_state.capture_candidate_state
        ).parameters
        self.assertNotIn("repo", parameters)
        self.assertNotIn("root", parameters)
        self.assertIn("store", parameters)
        self.assertIn("task_id", parameters)
        self.assertIn("baseline_commit", parameters)
        self.assertIn("runtime_evidence_ids", parameters)

    def test_valid_state_round_trips(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(_valid_task(self.host))
        evidence = "ab" * 32
        state = _capture(
            store,
            TASK_ID,
            baseline_commit=self.host_baseline,
            runtime_evidence_ids=(evidence, evidence),
        )
        payload = state.to_dict()
        candidate_state.validate_candidate_state(payload)
        self.assertEqual(candidate_state.CANDIDATE_STATE_FIELDS, set(payload))
        self.assertEqual("ai-candidate-state-1", payload["schema_version"])
        envelope = artifacts.load_artifact(store._require_task(TASK_ID) / "task.json")
        self.assertEqual(artifacts.artifact_sha256(envelope), payload["envelope_hash"])
        self.assertEqual([evidence, evidence], payload["runtime_evidence_ids"])

    def test_missing_field_is_rejected(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(_valid_task(self.host))
        payload = _capture(
            store, TASK_ID, baseline_commit=self.host_baseline
        ).to_dict()
        del payload["tree_digest"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            candidate_state.validate_candidate_state(payload)

    def test_empty_runtime_evidence_id_is_rejected(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(_valid_task(self.host))
        with self.assertRaisesRegex(artifacts.WorkflowError, "EMPTY_FIELD"):
            _capture(
                store,
                TASK_ID,
                baseline_commit=self.host_baseline,
                runtime_evidence_ids=("ab" * 32, ""),
            )
        payload = _capture(
            store, TASK_ID, baseline_commit=self.host_baseline
        ).to_dict()
        payload["runtime_evidence_ids"] = ["ab" * 32, ""]
        with self.assertRaisesRegex(artifacts.WorkflowError, "EMPTY_FIELD"):
            candidate_state.validate_candidate_state(payload)

    def test_envelope_hash_must_be_64_hex(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(_valid_task(self.host))
        payload = _capture(
            store, TASK_ID, baseline_commit=self.host_baseline
        ).to_dict()
        for value in ("", "abc", "g" * 64, "ab" * 31, "AB" * 32):
            mutated = dict(payload)
            mutated["envelope_hash"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                artifacts.WorkflowError, "INVALID_TYPE|EMPTY_FIELD"
            ):
                candidate_state.validate_candidate_state(mutated)

    def test_state_digest_ignores_captured_at_utc(self) -> None:
        store = workflow.WorkflowStore(self.host / "data" / "state" / "ai-workflow")
        store.create_task(_valid_task(self.host))
        state = _capture(store, TASK_ID, baseline_commit=self.host_baseline)
        payload = state.to_dict()
        payload["captured_at_utc"] = "1999-01-01T00:00:00Z"
        candidate_state.validate_candidate_state(payload)
        other = candidate_state.CandidateState(
            schema_version=str(payload["schema_version"]),
            task_id=str(payload["task_id"]),
            envelope_hash=str(payload["envelope_hash"]),
            candidate_commit=str(payload["candidate_commit"]),
            baseline_commit=str(payload["baseline_commit"]),
            tree_digest=str(payload["tree_digest"]),
            diff_digest=str(payload["diff_digest"]),
            runtime_evidence_ids=tuple(payload["runtime_evidence_ids"]),
            captured_at_utc=str(payload["captured_at_utc"]),
        )
        self.assertEqual(state.state_digest(), other.state_digest())
        self.assertNotEqual(state.captured_at_utc, other.captured_at_utc)
        self.assertEqual(64, len(state.state_digest()))

    def test_path_nfc_normalization(self) -> None:
        nfc = unicodedata.normalize("NFC", "café.txt")
        _write(self.host, nfc, "accent\n")
        _run_git(self.host, "add", "-A")
        _run_git(self.host, "commit", "-m", "nfc")
        paths = [
            entry.path
            for entry in candidate_state.scan_candidate_manifest(
                self.host, exclusions=_exclusions(self.host)
            )
        ]
        matching = [path for path in paths if path.casefold().endswith("caf\u00e9.txt".casefold()) or "cafe" in path.casefold()]
        self.assertTrue(matching)
        for path in matching:
            self.assertEqual(unicodedata.normalize("NFC", path), path)


if __name__ == "__main__":
    unittest.main()
