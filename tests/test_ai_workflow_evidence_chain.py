"""Read-only evidence-chain auditor: five links, gap codes, and CLI."""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_candidate_state as candidate_state
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_evidence as evidence
from scripts import ai_workflow_evidence_chain as chain
from scripts import ai_workflow_verdicts as verdicts
from tests.test_ai_workflow import _install_declaration
from tests.test_ai_workflow_launch_intent import _complete_observation
from tests.test_ai_workflow_verdicts import _issuer_evidence


ROOT = Path(__file__).resolve().parents[1]
PYTHON311 = Path("/Users/lee/.local/bin/python3.11")
TASK_ID = "AWF-20260803-001"
SCRIPTS = ROOT / "scripts"
PRODUCTION_MODULES = (
    "ai_workflow.py",
    "ai_workflow_repairs.py",
    "ai_workflow_declarations.py",
    "ai_workflow_candidate_state.py",
    "ai_workflow_verdicts.py",
    "ai_workflow_evidence.py",
    "ai_workflow_dispatch_policy.py",
    "ai_workflow_preflight.py",
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_snapshot(task_dir: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(task_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshot[str(path.relative_to(task_dir))] = _sha256_file(path)
    return snapshot


def _events(store: workflow.WorkflowStore, task_id: str) -> list[dict[str, object]]:
    return list(store.read_task_ledger(task_id, "events.jsonl"))


def _evidence_ids_from_events(store: workflow.WorkflowStore, task_id: str) -> tuple[str, ...]:
    ids: list[str] = []
    for event in store.read_task_ledger(task_id, "events.jsonl"):
        if event.get("event_type") != "RUNTIME_EVIDENCE_RECORDED":
            continue
        digest = event.get("runtime_evidence_sha256")
        if isinstance(digest, str) and digest:
            ids.append(digest)
    return tuple(ids)


def _link_by_name(built: chain.EvidenceChain, name: str) -> chain.EvidenceChainLink | None:
    return getattr(built, name)


class ContractTest(unittest.TestCase):
    def test_link_and_gap_constants_are_frozen(self) -> None:
        self.assertEqual(
            (
                "route_declaration",
                "launch_intent",
                "rollout_identity",
                "fork_state",
                "final_verdict",
            ),
            chain.EVIDENCE_CHAIN_LINKS,
        )
        self.assertEqual(
            frozenset(
                {
                    "CHAIN_MISSING_ROUTE_DECLARATION",
                    "CHAIN_ENVELOPE_MISMATCH",
                    "CHAIN_MISSING_LAUNCH_INTENT",
                    "CHAIN_MISSING_ROLLOUT_IDENTITY",
                    "CHAIN_FORK_STATE_UNVERIFIED",
                    "CHAIN_MISSING_FINAL_VERDICT",
                    "CHAIN_VERDICT_STALE",
                    "CHAIN_EVIDENCE_ORPHAN",
                }
            ),
            chain.EVIDENCE_CHAIN_GAP_CODES,
        )

    def test_build_evidence_chain_signature_has_no_caller_baseline(self) -> None:
        parameters = inspect.signature(chain.build_evidence_chain).parameters
        self.assertNotIn("current", parameters)
        self.assertNotIn("baseline_commit", parameters)
        self.assertEqual(("store", "task_id"), tuple(parameters))

    def test_production_modules_do_not_import_the_auditor(self) -> None:
        for name in PRODUCTION_MODULES:
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=name)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
                    imported.update(alias.name.split(".")[0] for alias in node.names)
            self.assertNotIn(
                "ai_workflow_evidence_chain",
                imported,
                f"{name} must not import the evidence-chain auditor",
            )


class _ChainFixtureMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repository"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "chain@example.test")
        _git(self.repo, "config", "user.name", "Chain Test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "init")
        self.baseline_commit = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        self.store = workflow.WorkflowStore(root / "state")
        self.task = {
            "schema_version": "ai-task-1",
            "task_id": TASK_ID,
            "task_type": "PLAN",
            "objective": "Review the approved workflow specification",
            "repository_root": str(self.repo),
            "source_worktree": None,
            "base_commit": None,
            "candidate_commit": None,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": [],
            "forbidden_actions": ["merge", "push", "change_constitution"],
            "risk_flags": [],
            "acceptance_commands": [],
            "verification_level": "L1",
            "human_gates": ["PLAN_APPROVAL"],
        }
        self.store.create_task(self.task)
        self.task = artifacts.load_artifact(self.store._require_task(TASK_ID) / "task.json")
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.task_dir = self.store._require_task(TASK_ID)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _populate(
        self,
        *,
        declaration: bool = True,
        launch_intent: bool = True,
        rollout: bool = True,
        v2_observation: dict[str, object] | None | bool = True,
        verdict: bool = True,
        orphan_id: bool = False,
    ) -> None:
        if declaration:
            _install_declaration(
                self.store,
                self.task,
                allowed_roles=("luna", "sol_medium_reviewer"),
                active_roles=("luna",),
            )
        if launch_intent:
            permit = policy.DispatchPermit(
                permit_id="b" * 64,
                task_id=TASK_ID,
                role="luna",
                reservation_seq=1,
            )
            with self.store.lock(TASK_ID):
                evidence.record_launch_intent(
                    self.store,
                    TASK_ID,
                    permit=permit,
                    role="luna",
                    argv=("codex", "exec"),
                    tool_mapping={},
                )
        issuer_id = ""
        if rollout:
            issuer = _issuer_evidence(observed_cwd=str(self.repo))
            issuer_id = artifacts.artifact_sha256(issuer)
            self.store.append_task_ledger(TASK_ID, "runtime-evidence.jsonl", issuer)
            self.store.append_event(
                TASK_ID,
                {
                    "event_type": "RUNTIME_EVIDENCE_RECORDED",
                    "attempt_id": issuer["attempt_id"],
                    "requested_role": issuer["requested_role"],
                    "runtime_evidence_sha256": issuer_id,
                },
            )
            event_index = len(_events(self.store, TASK_ID)) - 1
            if v2_observation is not False:
                observed = (
                    v2_observation
                    if isinstance(v2_observation, dict)
                    else _complete_observation()
                )
                evidence.append_runtime_evidence_v2(
                    self.store,
                    TASK_ID,
                    event_index=event_index,
                    observed=observed,
                    recorded_at_utc="2026-08-28T00:00:00Z",
                )
        if not verdict:
            return
        runtime_ids = list(_evidence_ids_from_events(self.store, TASK_ID))
        if orphan_id:
            junk = _issuer_evidence(attempt_id="attempt-orphan", observed_cwd=str(self.repo))
            junk_id = artifacts.artifact_sha256(junk)
            self.store.append_task_ledger(TASK_ID, "runtime-evidence.jsonl", junk)
            runtime_ids.append(junk_id)
        captured = candidate_state.capture_candidate_state(
            self.store,
            TASK_ID,
            baseline_commit=self.baseline_commit,
            runtime_evidence_ids=tuple(runtime_ids),
        )
        with self.store.lock(TASK_ID):
            verdicts.record_final_verdict(
                self.store,
                TASK_ID,
                verdict="ACCEPT",
                candidate_state=captured,
                issuer_evidence_id=issuer_id,
                recorded_at="2026-08-28T12:00:00Z",
            )


class CompleteChainTest(_ChainFixtureMixin, unittest.TestCase):
    def test_complete_chain_validates_empty_and_shares_envelope(self) -> None:
        self._populate()
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertEqual((), chain.validate_evidence_chain(built))
        self.assertEqual(TASK_ID, built.task_id)
        self.assertEqual(self.envelope_hash, built.envelope_hash)
        for name in chain.EVIDENCE_CHAIN_LINKS:
            link = _link_by_name(built, name)
            self.assertIsNotNone(link, name)
            assert link is not None
            self.assertEqual(TASK_ID, link.task_id)
            self.assertEqual(self.envelope_hash, link.envelope_hash)
            self.assertTrue(link.source_path or link.event_index is not None, name)
        declaration = built.route_declaration
        assert declaration is not None
        self.assertTrue(str(declaration.source_path).endswith("route-declaration.json"))
        intent = built.launch_intent
        assert intent is not None
        self.assertIsInstance(intent.event_index, int)
        rollout = built.rollout_identity
        assert rollout is not None
        self.assertIsInstance(rollout.event_index, int)
        fork = built.fork_state
        assert fork is not None
        self.assertTrue(str(fork.source_path).endswith("runtime-evidence-v2.jsonl"))
        self.assertEqual("VERIFIED_NONE", fork.fork_state)
        self.assertEqual("VERIFIED_NONE", fork.nested_state)
        final = built.final_verdict
        assert final is not None
        self.assertTrue(str(final.source_path).endswith("final-verdicts.jsonl"))


class GapDetectionTest(_ChainFixtureMixin, unittest.TestCase):
    def test_missing_declaration_is_reported(self) -> None:
        self._populate()
        (self.task_dir / declarations.DECLARATION_FILENAME).unlink()
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertIn(
            "CHAIN_MISSING_ROUTE_DECLARATION",
            chain.validate_evidence_chain(built),
        )

    def test_tampered_envelope_hash_is_mismatch(self) -> None:
        self._populate()
        built = chain.build_evidence_chain(self.store, TASK_ID)
        assert built.launch_intent is not None
        tampered = replace(
            built,
            launch_intent=replace(built.launch_intent, envelope_hash="0" * 64),
        )
        self.assertIn("CHAIN_ENVELOPE_MISMATCH", chain.validate_evidence_chain(tampered))

    def test_missing_launch_intent_is_reported(self) -> None:
        self._populate(launch_intent=False)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertIn(
            "CHAIN_MISSING_LAUNCH_INTENT",
            chain.validate_evidence_chain(built),
        )

    def test_missing_rollout_identity_is_reported(self) -> None:
        self._populate(rollout=False, verdict=False)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertIn(
            "CHAIN_MISSING_ROLLOUT_IDENTITY",
            chain.validate_evidence_chain(built),
        )

    def test_authority_unavailable_fork_is_unverified_not_verified_none(self) -> None:
        observed = {
            "observed_agent_type": None,
            "native_agent_id": None,
            "native_thread_id": None,
        }
        self._populate(v2_observation=observed)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        gaps = chain.validate_evidence_chain(built)
        self.assertIn("CHAIN_FORK_STATE_UNVERIFIED", gaps)
        self.assertNotIn("VERIFIED_NONE", gaps)
        assert built.fork_state is not None
        self.assertEqual("AUTHORITY_UNAVAILABLE", built.fork_state.fork_state)
        self.assertEqual("AUTHORITY_UNAVAILABLE", built.fork_state.nested_state)

    def test_missing_v2_observation_is_unverified_not_verified_none(self) -> None:
        self._populate(v2_observation=False)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        gaps = chain.validate_evidence_chain(built)
        self.assertIn("CHAIN_FORK_STATE_UNVERIFIED", gaps)
        self.assertIsNone(built.fork_state)

    def test_missing_final_verdict_is_reported_without_recapture(self) -> None:
        self._populate(verdict=False)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertIn(
            "CHAIN_MISSING_FINAL_VERDICT",
            chain.validate_evidence_chain(built),
        )

    def test_stale_verdict_is_reported(self) -> None:
        self._populate()
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        built = chain.build_evidence_chain(self.store, TASK_ID)
        self.assertIn("CHAIN_VERDICT_STALE", chain.validate_evidence_chain(built))

    def test_forged_verdict_evidence_id_is_orphan(self) -> None:
        self._populate(orphan_id=True)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        gaps = chain.validate_evidence_chain(built)
        self.assertIn("CHAIN_EVIDENCE_ORPHAN", gaps)


class ReadOnlyAndCliTest(_ChainFixtureMixin, unittest.TestCase):
    def test_build_and_validate_do_not_write_the_task_directory(self) -> None:
        self._populate()
        before = _task_snapshot(self.task_dir)
        built = chain.build_evidence_chain(self.store, TASK_ID)
        chain.validate_evidence_chain(built)
        after = _task_snapshot(self.task_dir)
        self.assertEqual(before, after)

    def test_cli_complete_chain_exits_zero_and_prints_five_links(self) -> None:
        self._populate()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = chain.main(
                ["--root", str(self.store.root), "--task-id", TASK_ID]
            )
        self.assertEqual(0, code)
        output = buffer.getvalue()
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 5)
        for name in chain.EVIDENCE_CHAIN_LINKS:
            self.assertTrue(
                any(name in line for line in lines),
                f"missing status line for {name} in {output!r}",
            )

    def test_cli_subprocess_complete_chain_exits_zero(self) -> None:
        self._populate()
        result = subprocess.run(
            [
                str(PYTHON311),
                str(SCRIPTS / "ai_workflow_evidence_chain.py"),
                "--root",
                str(self.store.root),
                "--task-id",
                TASK_ID,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for name in chain.EVIDENCE_CHAIN_LINKS:
            self.assertIn(name, result.stdout)

    def test_cli_gap_chain_exits_one_and_prints_gap_codes(self) -> None:
        self._populate(launch_intent=False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = chain.main(
                ["--root", str(self.store.root), "--task-id", TASK_ID]
            )
        self.assertEqual(1, code)
        output = buffer.getvalue()
        self.assertIn("CHAIN_MISSING_LAUNCH_INTENT", output)
        codes = [
            line.strip()
            for line in output.splitlines()
            if line.strip() in chain.EVIDENCE_CHAIN_GAP_CODES
        ]
        self.assertIn("CHAIN_MISSING_LAUNCH_INTENT", codes)

    def test_cli_subprocess_gap_chain_exits_one(self) -> None:
        self._populate(launch_intent=False)
        result = subprocess.run(
            [
                str(PYTHON311),
                str(SCRIPTS / "ai_workflow_evidence_chain.py"),
                "--root",
                str(self.store.root),
                "--task-id",
                TASK_ID,
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("CHAIN_MISSING_LAUNCH_INTENT", result.stdout)


if __name__ == "__main__":
    unittest.main()
