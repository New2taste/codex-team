"""Owner authorization sidecar: per-kind IDs, consumption, and transfer leases."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_authorizations as authorizations


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
LEDGER_NAME = "owner-authorizations.jsonl"
GOLDEN_AUTHORIZATION_ID = (
    "350cfbaf207bdcc4ad88ea03245a80ade174b4b2a7884000116dd7252a3094fb"
)
GOLDEN_CONSUMPTION_RECORD_ID = (
    "732396bf15bbc0423dae2736978b8d13675033522a159ab28014be5ce0956f0b"
)
GOLDEN_LEASE_RECORD_ID = (
    "af806b1479bd6f6719c2760a1beb0f75d79c4a6e60aa7457fca910cd3ddb9203"
)
FROZEN_OWNER_DECISIONS = frozenset(
    {
        "approve_execution",
        "authorize_rework",
        "authorize_escalation",
        "defer",
        "close",
        "abort",
    }
)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64
DIGEST_F = "f" * 64
CANDIDATE_STATE_DIGEST = DIGEST_B


def _valid_task(*, task_id: str = TASK_ID) -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "PLAN",
        "objective": "Issue a scoped owner authorization",
        "repository_root": str(ROOT),
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


def _golden_authorization(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "ai-owner-authorization-1",
        "record_kind": "authorization",
        "authorization_id": "deadbeef" * 8,
        "authorization_type": "VERDICT_STALE_OVERRIDE",
        "task_id": TASK_ID,
        "envelope_hash": DIGEST_A,
        "candidate_state_digest": DIGEST_B,
        "actor": "owner",
        "owner_evidence_id": DIGEST_C,
        "issued_at_utc": "2026-08-28T00:00:00Z",
    }
    record.update(overrides)
    return record


def _golden_consumption(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "ai-owner-authorization-1",
        "record_kind": "consumption",
        "record_id": "deadbeef" * 8,
        "authorization_id": DIGEST_D,
        "task_id": TASK_ID,
        "envelope_hash": DIGEST_A,
        "binding": {"candidate_state_digest": DIGEST_B},
        "issued_at_utc": "2026-08-28T00:00:00Z",
    }
    record.update(overrides)
    return record


def _golden_lease(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "ai-owner-authorization-1",
        "record_kind": "transfer_lease",
        "record_id": "deadbeef" * 8,
        "authorization_id": DIGEST_D,
        "task_id": TASK_ID,
        "envelope_hash": DIGEST_A,
        "permit_id": DIGEST_E,
        "dispatch_seq": 1,
        "allowed_paths": ["src/b.py", "src/a.py"],
        "issued_at_utc": "2026-08-28T00:00:00Z",
    }
    record.update(overrides)
    return record


def _decision_record(*, actor: str = "owner") -> dict[str, object]:
    return {
        "event_type": "OWNER_DECISION",
        "decision": "defer",
        "actor": actor,
        "timestamp_utc": "2026-08-28T00:00:00Z",
        "previous_state": "AWAITING_OWNER_DECISION",
        "new_state": "DEFERRED",
        "task_sha256": DIGEST_A,
    }


def _first_call_name(function) -> str | None:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    for node in func.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
        else:
            return None
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return None
    return None


def _calls_store_lock(function) -> bool:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == "lock":
            return True
    return False


class GoldenPreimageTest(unittest.TestCase):
    def test_exclude_constants_are_self_only(self) -> None:
        self.assertEqual(
            frozenset({"authorization_id"}),
            authorizations.AUTHORIZATION_ID_EXCLUDE,
        )
        self.assertEqual(frozenset({"record_id"}), authorizations.RECORD_ID_EXCLUDE)
        self.assertFalse(hasattr(authorizations, "OWNER_AUTH_ID_EXCLUDE"))
        source = inspect.getsource(authorizations)
        self.assertNotIn("OWNER_AUTH_ID_EXCLUDE", source)

    def test_frozen_authorization_hashes_to_literal_golden(self) -> None:
        computed = authorizations.compute_authorization_id(_golden_authorization())
        self.assertEqual(GOLDEN_AUTHORIZATION_ID, computed)
        self.assertEqual(64, len(computed))

    def test_frozen_consumption_hashes_to_literal_golden(self) -> None:
        computed = authorizations.compute_record_id(_golden_consumption())
        self.assertEqual(GOLDEN_CONSUMPTION_RECORD_ID, computed)
        self.assertEqual(64, len(computed))

    def test_frozen_lease_hashes_to_literal_golden(self) -> None:
        computed = authorizations.compute_record_id(_golden_lease())
        self.assertEqual(GOLDEN_LEASE_RECORD_ID, computed)
        self.assertEqual(64, len(computed))

    def test_prefilled_authorization_id_does_not_affect_compute(self) -> None:
        left = authorizations.compute_authorization_id(
            _golden_authorization(authorization_id="deadbeef" * 8)
        )
        right = authorizations.compute_authorization_id(
            _golden_authorization(authorization_id="cafebabe" * 8)
        )
        self.assertEqual(GOLDEN_AUTHORIZATION_ID, left)
        self.assertEqual(left, right)

    def test_prefilled_record_id_does_not_affect_compute(self) -> None:
        left = authorizations.compute_record_id(
            _golden_consumption(record_id="deadbeef" * 8)
        )
        right = authorizations.compute_record_id(
            _golden_lease(record_id="cafebabe" * 8)
        )
        self.assertEqual(GOLDEN_CONSUMPTION_RECORD_ID, left)
        self.assertEqual(GOLDEN_LEASE_RECORD_ID, right)

    def test_changing_only_authorization_id_mismatches_record_id(self) -> None:
        consumption = _golden_consumption()
        consumption["record_id"] = GOLDEN_CONSUMPTION_RECORD_ID
        authorizations.verify_record_id(consumption)
        consumption["authorization_id"] = DIGEST_F
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            authorizations.verify_record_id(consumption)

        lease = _golden_lease()
        lease["record_id"] = GOLDEN_LEASE_RECORD_ID
        authorizations.verify_record_id(lease)
        lease["authorization_id"] = DIGEST_F
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            authorizations.verify_record_id(lease)

    def test_lease_allowed_paths_order_and_dupes_share_projection(self) -> None:
        left = _golden_lease(allowed_paths=["src/b.py", "src/a.py", "src/a.py"])
        right = _golden_lease(allowed_paths=["src/a.py", "src/b.py"])
        self.assertEqual(
            authorizations.compute_record_id(left),
            authorizations.compute_record_id(right),
        )
        self.assertEqual(GOLDEN_LEASE_RECORD_ID, authorizations.compute_record_id(left))
        left["record_id"] = GOLDEN_LEASE_RECORD_ID
        authorizations.verify_record_id(left)
        right["record_id"] = GOLDEN_LEASE_RECORD_ID
        authorizations.verify_record_id(right)

    def test_mismatched_exclude_hash_fails_verify(self) -> None:
        authorization = _golden_authorization()
        wrong = artifacts.content_id(
            "ai-owner-authorization-1",
            authorization,
            exclude=frozenset({"authorization_id", "actor"}),
        )
        authorization["authorization_id"] = wrong
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            authorizations.verify_authorization_id(authorization)

        consumption = _golden_consumption()
        wrong_record = artifacts.content_id(
            "ai-owner-authorization-1",
            consumption,
            exclude=frozenset({"record_id", "authorization_id"}),
        )
        consumption["record_id"] = wrong_record
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            authorizations.verify_record_id(consumption)

    def test_verify_accepts_matching_authorization_id(self) -> None:
        record = _golden_authorization()
        record["authorization_id"] = GOLDEN_AUTHORIZATION_ID
        authorizations.verify_authorization_id(record)

    def test_field_vocabulary_is_the_union(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "schema_version",
                    "record_kind",
                    "authorization_id",
                    "record_id",
                    "authorization_type",
                    "task_id",
                    "envelope_hash",
                    "candidate_state_digest",
                    "path",
                    "from_role",
                    "to_role",
                    "allowed_paths",
                    "max_dispatches",
                    "permit_id",
                    "dispatch_seq",
                    "binding",
                    "actor",
                    "owner_evidence_id",
                    "issued_at_utc",
                }
            ),
            authorizations.OWNER_AUTHORIZATION_FIELDS,
        )
        self.assertEqual(
            "ai-owner-authorization-1",
            authorizations.OWNER_AUTHORIZATION_SCHEMA_VERSION,
        )
        self.assertEqual(
            frozenset({"VERDICT_STALE_OVERRIDE", "OWNERSHIP_TRANSFER"}),
            authorizations.AUTHORIZATION_TYPES,
        )
        self.assertEqual(
            frozenset({"authorization", "consumption", "transfer_lease"}),
            authorizations.AUTHORIZATION_RECORD_KINDS,
        )


class WireClosedSetTest(unittest.TestCase):
    def test_authorization_rejects_inapplicable_fields_even_when_null_or_empty(
        self,
    ) -> None:
        for field, value in (
            ("record_id", None),
            ("record_id", ""),
            ("permit_id", None),
            ("dispatch_seq", None),
            ("binding", None),
            ("binding", {}),
        ):
            with self.subTest(field=field, value=value):
                record = _golden_authorization(authorization_id=GOLDEN_AUTHORIZATION_ID)
                record[field] = value
                with self.assertRaisesRegex(artifacts.WorkflowError, "UNKNOWN_FIELD"):
                    authorizations.validate_owner_authorization(record)

    def test_authorization_rejects_other_type_scope_fields(self) -> None:
        record = _golden_authorization(authorization_id=GOLDEN_AUTHORIZATION_ID)
        record["path"] = "src/a.py"
        with self.assertRaisesRegex(artifacts.WorkflowError, "UNKNOWN_FIELD"):
            authorizations.validate_owner_authorization(record)

    def test_consumption_missing_authorization_id_or_binding_is_rejected(
        self,
    ) -> None:
        missing_auth = _golden_consumption()
        del missing_auth["authorization_id"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            authorizations.validate_owner_authorization(missing_auth)
        missing_binding = _golden_consumption()
        del missing_binding["binding"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            authorizations.validate_owner_authorization(missing_binding)

    def test_lease_missing_permit_id_or_dispatch_seq_is_rejected(self) -> None:
        missing_permit = _golden_lease()
        del missing_permit["permit_id"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            authorizations.validate_owner_authorization(missing_permit)
        missing_seq = _golden_lease()
        del missing_seq["dispatch_seq"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            authorizations.validate_owner_authorization(missing_seq)

    def test_type_outside_closed_set_is_rejected(self) -> None:
        record = _golden_authorization(authorization_id=GOLDEN_AUTHORIZATION_ID)
        record["authorization_type"] = "MERGE"
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            authorizations.validate_owner_authorization(record)

    def test_override_missing_candidate_state_digest_is_rejected(self) -> None:
        record = _golden_authorization(authorization_id=GOLDEN_AUTHORIZATION_ID)
        del record["candidate_state_digest"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            authorizations.validate_owner_authorization(record)

    def test_transfer_missing_scope_fields_is_rejected(self) -> None:
        record = {
            "schema_version": "ai-owner-authorization-1",
            "record_kind": "authorization",
            "authorization_id": "deadbeef" * 8,
            "authorization_type": "OWNERSHIP_TRANSFER",
            "task_id": TASK_ID,
            "envelope_hash": DIGEST_A,
            "path": "src/a.py",
            "from_role": "terra",
            "to_role": "luna",
            "allowed_paths": ["src/a.py"],
            "max_dispatches": 1,
            "actor": "owner",
            "owner_evidence_id": DIGEST_C,
            "issued_at_utc": "2026-08-28T00:00:00Z",
        }
        record["authorization_id"] = authorizations.compute_authorization_id(record)
        authorizations.validate_owner_authorization(record)
        for field in ("path", "from_role", "to_role", "allowed_paths", "max_dispatches"):
            with self.subTest(field=field):
                broken = dict(record)
                del broken[field]
                with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
                    authorizations.validate_owner_authorization(broken)

    def test_max_dispatches_rejects_bool_and_non_positive(self) -> None:
        base = {
            "schema_version": "ai-owner-authorization-1",
            "record_kind": "authorization",
            "authorization_id": "deadbeef" * 8,
            "authorization_type": "OWNERSHIP_TRANSFER",
            "task_id": TASK_ID,
            "envelope_hash": DIGEST_A,
            "path": "src/a.py",
            "from_role": "terra",
            "to_role": "luna",
            "allowed_paths": ["src/a.py"],
            "actor": "owner",
            "owner_evidence_id": DIGEST_C,
            "issued_at_utc": "2026-08-28T00:00:00Z",
        }
        for value in (True, False, 0, -1, 1.5, "2"):
            with self.subTest(value=value):
                record = dict(base)
                record["max_dispatches"] = value
                record["authorization_id"] = "deadbeef" * 8
                with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_TYPE"):
                    authorizations.validate_owner_authorization(record)

    def test_consumption_rejects_authorization_scope_fields(self) -> None:
        record = _golden_consumption()
        record["record_id"] = GOLDEN_CONSUMPTION_RECORD_ID
        record["actor"] = "owner"
        with self.assertRaisesRegex(artifacts.WorkflowError, "UNKNOWN_FIELD"):
            authorizations.validate_owner_authorization(record)


class _AuthorizationStoreMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task = _valid_task()
        self.store.create_task(self.task)
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.actor = "owner"
        self.evidence = _decision_record(actor=self.actor)
        self.store.record_decision(TASK_ID, self.evidence)
        self.owner_evidence_id = artifacts.artifact_sha256(self.evidence)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _issue_override(self, **overrides: object) -> authorizations.OwnerAuthorization:
        kwargs: dict[str, object] = {
            "authorization_type": "VERDICT_STALE_OVERRIDE",
            "actor": self.actor,
            "owner_evidence_id": self.owner_evidence_id,
            "issued_at_utc": "2026-08-28T12:00:00Z",
            "candidate_state_digest": CANDIDATE_STATE_DIGEST,
        }
        kwargs.update(overrides)
        return authorizations.issue_owner_authorization(
            self.store, TASK_ID, **kwargs
        )

    def _issue_transfer(self, **overrides: object) -> authorizations.OwnerAuthorization:
        kwargs: dict[str, object] = {
            "authorization_type": "OWNERSHIP_TRANSFER",
            "actor": self.actor,
            "owner_evidence_id": self.owner_evidence_id,
            "issued_at_utc": "2026-08-28T12:00:00Z",
            "path": "src/a.py",
            "from_role": "terra",
            "to_role": "luna",
            "allowed_paths": ("src/b.py", "src/a.py"),
            "max_dispatches": 2,
        }
        kwargs.update(overrides)
        return authorizations.issue_owner_authorization(
            self.store, TASK_ID, **kwargs
        )

    def _ledger_path(self) -> Path:
        return self.store._require_task(TASK_ID) / LEDGER_NAME

    def _override_binding(self) -> dict[str, object]:
        return {"candidate_state_digest": CANDIDATE_STATE_DIGEST}


class IssueRoundTripTest(_AuthorizationStoreMixin, unittest.TestCase):
    def test_override_and_transfer_round_trip(self) -> None:
        override = self._issue_override()
        loaded = authorizations.load_owner_authorization(
            self.store, TASK_ID, override.authorization_id
        )
        self.assertIsNotNone(loaded)
        assert loaded is not None
        payload = loaded.to_dict()
        self.assertNotIn("record_id", payload)
        self.assertNotIn("permit_id", payload)
        self.assertNotIn("dispatch_seq", payload)
        self.assertNotIn("binding", payload)
        self.assertNotIn("path", payload)
        self.assertEqual("VERDICT_STALE_OVERRIDE", payload["authorization_type"])
        self.assertEqual(CANDIDATE_STATE_DIGEST, payload["candidate_state_digest"])
        self.assertEqual(self.envelope_hash, payload["envelope_hash"])
        self.assertEqual(override.to_dict(), payload)
        authorizations.verify_authorization_id(payload)
        authorizations.validate_owner_authorization(payload)

        transfer = self._issue_transfer()
        loaded_transfer = authorizations.load_owner_authorization(
            self.store, TASK_ID, transfer.authorization_id
        )
        self.assertIsNotNone(loaded_transfer)
        assert loaded_transfer is not None
        transfer_payload = loaded_transfer.to_dict()
        self.assertNotIn("candidate_state_digest", transfer_payload)
        self.assertNotIn("record_id", transfer_payload)
        self.assertEqual(["src/a.py", "src/b.py"], transfer_payload["allowed_paths"])
        self.assertEqual(2, transfer_payload["max_dispatches"])
        authorizations.verify_authorization_id(transfer_payload)
        authorizations.validate_owner_authorization(transfer_payload)

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            self._issue_override(authorization_type="MERGE")

    def test_override_requires_candidate_state_digest(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            self._issue_override(candidate_state_digest=None)

    def test_transfer_requires_scope(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            self._issue_transfer(path=None)

    def test_unknown_owner_evidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_EVIDENCE_UNKNOWN"
        ):
            self._issue_override(owner_evidence_id=DIGEST_F)

    def test_owner_evidence_actor_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_EVIDENCE_ACTOR_MISMATCH"
        ):
            self._issue_override(actor="other")

    def test_max_dispatches_bool_is_rejected_on_issue(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_TYPE"):
            self._issue_transfer(max_dispatches=True)

    def test_load_returns_none_for_unknown_id(self) -> None:
        self.assertIsNone(
            authorizations.load_owner_authorization(self.store, TASK_ID, DIGEST_F)
        )

    def test_readers_never_take_the_lock(self) -> None:
        for function in (
            authorizations.load_owner_authorization,
            authorizations.count_transfer_leases,
            authorizations.leases_for_permit,
            authorizations.replay_authorizations,
        ):
            with self.subTest(function=function.__name__):
                self.assertFalse(_calls_store_lock(function))


class ConsumeAndLockTest(_AuthorizationStoreMixin, unittest.TestCase):
    def test_consume_appends_binding_and_rejects_second_use(self) -> None:
        issued = self._issue_override()
        consumed = authorizations.consume_owner_authorization(
            self.store,
            TASK_ID,
            issued.authorization_id,
            binding=self._override_binding(),
        )
        self.assertEqual(issued.authorization_id, consumed.authorization_id)
        rows = authorizations.replay_authorizations(self.store, TASK_ID)
        consumptions = [row for row in rows if row["record_kind"] == "consumption"]
        self.assertEqual(1, len(consumptions))
        self.assertEqual(issued.authorization_id, consumptions[0]["authorization_id"])
        self.assertEqual(self._override_binding(), consumptions[0]["binding"])
        self.assertIn("record_id", consumptions[0])
        authorizations.verify_record_id(consumptions[0])

        with self.assertRaisesRegex(artifacts.WorkflowError, "AUTHORIZATION_CONSUMED"):
            authorizations.consume_owner_authorization(
                self.store,
                TASK_ID,
                issued.authorization_id,
                binding=self._override_binding(),
            )
        self.assertEqual(
            1,
            sum(
                1
                for row in authorizations.replay_authorizations(self.store, TASK_ID)
                if row["record_kind"] == "consumption"
            ),
        )

    def test_unknown_authorization_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "AUTHORIZATION_UNKNOWN"):
            authorizations.consume_owner_authorization(
                self.store,
                TASK_ID,
                DIGEST_F,
                binding=self._override_binding(),
            )

    def test_binding_mismatch_does_not_consume(self) -> None:
        issued = self._issue_override()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
        ):
            authorizations.consume_owner_authorization(
                self.store,
                TASK_ID,
                issued.authorization_id,
                binding={"candidate_state_digest": DIGEST_F},
            )
        rows = authorizations.replay_authorizations(self.store, TASK_ID)
        self.assertFalse(any(row["record_kind"] == "consumption" for row in rows))

    def test_locked_variant_requires_held_lock(self) -> None:
        issued = self._issue_override()
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            authorizations.consume_owner_authorization_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                binding=self._override_binding(),
            )
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(authorizations.consume_owner_authorization_locked),
        )

    def test_self_locking_wrapper_fails_when_lock_already_held(self) -> None:
        issued = self._issue_override()
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                authorizations.consume_owner_authorization(
                    self.store,
                    TASK_ID,
                    issued.authorization_id,
                    binding=self._override_binding(),
                )
        tree = ast.parse(inspect.getsource(authorizations.consume_owner_authorization))
        func = tree.body[0]
        stmts = [
            node
            for node in func.body
            if not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            )
        ]
        self.assertEqual(1, len(stmts))
        self.assertIsInstance(stmts[0], ast.With)

    def test_dual_thread_consume_succeeds_once(self) -> None:
        issued = self._issue_override()
        results: list[str] = []

        def worker() -> None:
            while True:
                try:
                    authorizations.consume_owner_authorization(
                        self.store,
                        TASK_ID,
                        issued.authorization_id,
                        binding=self._override_binding(),
                    )
                    results.append("ok")
                    return
                except artifacts.WorkflowError as exc:
                    if exc.code == "TASK_ALREADY_RUNNING":
                        continue
                    results.append(exc.code)
                    return

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual({"ok", "AUTHORIZATION_CONSUMED"}, set(results))
        self.assertEqual(
            1,
            sum(
                1
                for row in authorizations.replay_authorizations(self.store, TASK_ID)
                if row["record_kind"] == "consumption"
            ),
        )


class TransferLeaseTest(_AuthorizationStoreMixin, unittest.TestCase):
    def test_leases_increment_local_dispatch_seq_then_exhaust(self) -> None:
        issued = self._issue_transfer(max_dispatches=2)
        with self.store.lock(TASK_ID):
            first = authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-1",
                paths=("src/b.py", "src/a.py"),
            )
            second = authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-2",
                paths=("src/a.py",),
            )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "AUTHORIZATION_EXHAUSTED"
            ):
                authorizations.record_transfer_lease_locked(
                    self.store,
                    TASK_ID,
                    issued.authorization_id,
                    permit_id="permit-3",
                    paths=("src/a.py",),
                )
        self.assertEqual(1, first["dispatch_seq"])
        self.assertEqual(2, second["dispatch_seq"])
        self.assertEqual(["src/a.py", "src/b.py"], first["allowed_paths"])
        self.assertEqual(["src/a.py"], second["allowed_paths"])
        self.assertEqual("permit-1", first["permit_id"])
        self.assertEqual("permit-2", second["permit_id"])
        self.assertEqual(
            2,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )
        authorizations.verify_record_id(first)
        authorizations.verify_record_id(second)

    def test_claimed_paths_must_be_subset(self) -> None:
        issued = self._issue_transfer()
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
            ):
                authorizations.record_transfer_lease_locked(
                    self.store,
                    TASK_ID,
                    issued.authorization_id,
                    permit_id="permit-x",
                    paths=("docs/note.md",),
                )
        self.assertEqual(
            0,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )

    def test_override_cannot_record_transfer_lease(self) -> None:
        issued = self._issue_override()
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
            ):
                authorizations.record_transfer_lease_locked(
                    self.store,
                    TASK_ID,
                    issued.authorization_id,
                    permit_id="permit-x",
                    paths=("src/a.py",),
                )

    def test_leases_for_permit_does_not_merge_other_permits(self) -> None:
        issued = self._issue_transfer(max_dispatches=2)
        with self.store.lock(TASK_ID):
            authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-a",
                paths=("src/a.py",),
            )
            authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-b",
                paths=("src/b.py",),
            )
        left = authorizations.leases_for_permit(self.store, TASK_ID, "permit-a")
        right = authorizations.leases_for_permit(self.store, TASK_ID, "permit-b")
        self.assertEqual(1, len(left))
        self.assertEqual(1, len(right))
        self.assertEqual("permit-a", left[0]["permit_id"])
        self.assertEqual("permit-b", right[0]["permit_id"])
        self.assertEqual(["src/a.py"], left[0]["allowed_paths"])
        self.assertEqual(["src/b.py"], right[0]["allowed_paths"])
        self.assertNotEqual(left[0]["record_id"], right[0]["record_id"])

    def test_locked_lease_requires_held_lock(self) -> None:
        issued = self._issue_transfer()
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-x",
                paths=("src/a.py",),
            )
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(authorizations.record_transfer_lease_locked),
        )

    def test_dual_thread_lease_does_not_over_issue(self) -> None:
        issued = self._issue_transfer(max_dispatches=1)
        results: list[str] = []

        def worker() -> None:
            while True:
                try:
                    with self.store.lock(TASK_ID):
                        authorizations.record_transfer_lease_locked(
                            self.store,
                            TASK_ID,
                            issued.authorization_id,
                            permit_id="permit-race",
                            paths=("src/a.py",),
                        )
                    results.append("ok")
                    return
                except artifacts.WorkflowError as exc:
                    if exc.code == "TASK_ALREADY_RUNNING":
                        continue
                    results.append(exc.code)
                    return

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual({"ok", "AUTHORIZATION_EXHAUSTED"}, set(results))
        self.assertEqual(
            1,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )


class ReplayAndClosedSetTest(_AuthorizationStoreMixin, unittest.TestCase):
    def _write_ledger(self, raw: bytes) -> None:
        self._ledger_path().write_bytes(raw)

    def test_truncated_trailing_record_is_corrupt(self) -> None:
        issued = self._issue_override()
        raw = self._ledger_path().read_bytes().rstrip(b"\n")
        self._write_ledger(raw)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)
        self.assertIsNotNone(issued)

    def test_non_object_line_is_corrupt(self) -> None:
        self._issue_override()
        raw = self._ledger_path().read_bytes()
        self._write_ledger(raw + b"[]\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_foreign_task_id_is_corrupt(self) -> None:
        self._issue_override()
        foreign = _golden_authorization(task_id=OTHER_TASK_ID)
        foreign["authorization_id"] = authorizations.compute_authorization_id(foreign)
        with self._ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(artifacts.canonical_json(foreign) + "\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_duplicate_record_id_is_corrupt(self) -> None:
        issued = self._issue_override()
        authorizations.consume_owner_authorization(
            self.store,
            TASK_ID,
            issued.authorization_id,
            binding=self._override_binding(),
        )
        rows = [
            row
            for row in authorizations.replay_authorizations(self.store, TASK_ID)
            if row["record_kind"] == "consumption"
        ]
        raw = self._ledger_path().read_bytes()
        self._write_ledger(raw + (artifacts.canonical_json(rows[0]) + "\n").encode())
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_duplicate_authorization_record_is_corrupt(self) -> None:
        issued = self._issue_override()
        payload = issued.to_dict()
        raw = self._ledger_path().read_bytes()
        self._write_ledger(raw + (artifacts.canonical_json(payload) + "\n").encode())
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_local_dispatch_seq_gap_is_corrupt(self) -> None:
        issued = self._issue_transfer(max_dispatches=2)
        with self.store.lock(TASK_ID):
            first = authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-1",
                paths=("src/a.py",),
            )
        gap = dict(first)
        gap["dispatch_seq"] = 3
        gap["permit_id"] = "permit-gap"
        gap["record_id"] = authorizations.compute_record_id(gap)
        with self._ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(artifacts.canonical_json(gap) + "\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_local_dispatch_seq_repeat_is_corrupt(self) -> None:
        issued = self._issue_transfer(max_dispatches=2)
        with self.store.lock(TASK_ID):
            first = authorizations.record_transfer_lease_locked(
                self.store,
                TASK_ID,
                issued.authorization_id,
                permit_id="permit-1",
                paths=("src/a.py",),
            )
        repeated = dict(first)
        repeated["permit_id"] = "permit-repeat"
        repeated["record_id"] = authorizations.compute_record_id(repeated)
        with self._ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(artifacts.canonical_json(repeated) + "\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_LEDGER_CORRUPT"
        ):
            authorizations.replay_authorizations(self.store, TASK_ID)

    def test_ledger_record_has_no_global_seq_field(self) -> None:
        self.assertNotIn("seq", authorizations.OWNER_AUTHORIZATION_FIELDS)
        self.assertFalse(hasattr(authorizations.OwnerAuthorization, "seq"))
        self.assertNotIn(
            "seq", inspect.signature(authorizations.replay_authorizations).parameters
        )

    def test_owner_decisions_closed_set_is_unchanged(self) -> None:
        self.assertEqual(FROZEN_OWNER_DECISIONS, workflow.OWNER_DECISIONS)
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", Path(self.temporary.name)):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "INVALID_OWNER_DECISION"
            ):
                workflow.apply_owner_decision(
                    TASK_ID, "VERDICT_STALE_OVERRIDE", "owner"
                )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "INVALID_OWNER_DECISION"
            ):
                workflow.apply_owner_decision(
                    TASK_ID, "OWNERSHIP_TRANSFER", "owner"
                )


if __name__ == "__main__":
    unittest.main()
