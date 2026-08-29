import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow
from scripts import ai_workflow_costs as costs


ROOT = Path(__file__).resolve().parents[1]


def cost_record(**overrides):
    """Return one canonical cost-attempt record for focused tests."""

    value = {
        "schema_version": "cost-evidence-1",
        "route": "delegated",
        "role": "luna",
        "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
        "duration_seconds": None,
        "prompt_bytes": 100,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "retry_kind": "none",
        "verification_seconds": 0.0,
        "quality_outcome": "SUPPORTED",
        "paired_case_id": "case-01",
        "evidence_class": None,
        "rate_snapshot_id": None,
    }
    aliases = {
        "pair": "paired_case_id",
        "surface": "execution_surface",
        "status": "quality_outcome",
    }
    for key, target in aliases.items():
        if key in overrides:
            overrides[target] = overrides.pop(key)
    value.update(overrides)
    return value


class CostNormalizationTest(unittest.TestCase):
    def test_missing_tokens_remain_unavailable(self):
        evidence = workflow.normalize_cost_evidence(
            cost_record(input_tokens=None, output_tokens=None)
        )
        self.assertEqual("unavailable", evidence.evidence_class)
        self.assertIsNone(evidence.input_tokens)
        self.assertIsNone(evidence.output_tokens)

    def test_invalid_numbers_are_rejected(self):
        for value in (-1, True, float("nan"), "100"):
            with self.subTest(value=value), self.assertRaisesRegex(
                workflow.WorkflowError, "COST_EVIDENCE_INVALID"
            ):
                workflow.normalize_cost_evidence(cost_record(input_tokens=value))

    def test_explicit_duration_is_measured_even_when_token_usage_is_missing(self):
        evidence = workflow.normalize_cost_evidence(
            cost_record(
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                duration_seconds=2.5,
                evidence_class="measured",
            )
        )
        self.assertEqual("measured", evidence.evidence_class)

    def test_explicit_unavailable_with_duration_stays_unavailable(self):
        evidence = workflow.normalize_cost_evidence(
            cost_record(
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                duration_seconds=2.5,
                evidence_class="unavailable",
            )
        )
        self.assertEqual("unavailable", evidence.evidence_class)
        summary = workflow.aggregate_paired_cases([cost_record(
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            duration_seconds=2.5,
            evidence_class="unavailable",
        )])
        self.assertEqual(["unavailable"], summary["case-01"]["evidence_classes"])
        self.assertEqual(0, summary["case-01"]["measured_attempt_count"])
        self.assertEqual(1, summary["case-01"]["unavailable_attempt_count"])

    def test_projection_requires_rate_snapshot_and_keeps_price_separate(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "COST_EVIDENCE_INVALID"):
            workflow.normalize_cost_evidence(
                cost_record(evidence_class="sample_validated_projection")
            )
        evidence = workflow.normalize_cost_evidence(
            cost_record(
                evidence_class="sample_validated_projection",
                rate_snapshot_id="rates-2026-08-03",
                projected_cost_usd=0.25,
            )
        )
        self.assertEqual("sample_validated_projection", evidence.evidence_class)
        self.assertEqual("rates-2026-08-03", evidence.rate_snapshot_id)
        self.assertNotIn("projected_cost_usd", evidence.to_dict())

    def test_unavailable_rejects_projected_price_values(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "COST_EVIDENCE_INVALID"):
            workflow.normalize_cost_evidence(cost_record(projected_cost_usd=0.25))


class PairingAndClaimGateTest(unittest.TestCase):
    def test_failed_and_retry_attempts_count_in_the_same_pair(self):
        summary = workflow.aggregate_paired_cases(
            [
                cost_record(
                    pair="case-01",
                    role="sol_planner",
                    status="FAILED",
                    input_tokens=10,
                ),
                cost_record(
                    pair="case-01",
                    role="sol_planner",
                    retry_kind="technical",
                    input_tokens=12,
                ),
                cost_record(
                    pair="case-01",
                    role="luna",
                    surface="NATIVE_SUBAGENT",
                    input_tokens=5,
                ),
            ]
        )
        self.assertEqual(27, summary["case-01"]["measured_input_tokens"])
        self.assertEqual(1, summary["case-01"]["technical_retries"])
        self.assertEqual(
            {"NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"},
            set(summary["case-01"]["surfaces"]),
        )
        self.assertEqual(3, summary["case-01"]["attempt_count"])

    def test_claim_gate_requires_cases_quality_and_measured_delta(self):
        self.assertEqual(
            "OBSERVATION_ONLY",
            workflow.evaluate_cost_claim(
                {
                    "paired_case_count": 29,
                    "quality_delta_points": 0.0,
                    "net_measured_cost_delta": -1.0,
                }
            ),
        )
        self.assertEqual(
            "QUALITY_REGRESSION",
            workflow.evaluate_cost_claim(
                {
                    "paired_case_count": 30,
                    "quality_delta_points": -5.01,
                    "net_measured_cost_delta": -1.0,
                }
            ),
        )
        self.assertEqual(
            "NO_COST_REDUCTION_PROVEN",
            workflow.evaluate_cost_claim(
                {
                    "paired_case_count": 30,
                    "quality_delta_points": -5.0,
                    "net_measured_cost_delta": None,
                }
            ),
        )
        self.assertEqual(
            "COST_REDUCTION_SUPPORTED",
            workflow.evaluate_cost_claim(
                {
                    "paired_case_count": 30,
                    "quality_delta_points": 0.0,
                    "net_measured_cost_delta": -1.0,
                }
            ),
        )

    def test_signed_cost_and_quality_deltas_survive_pairing_and_claim_gate(self):
        summary = workflow.aggregate_paired_cases(
            [
                cost_record(
                    input_tokens=10,
                    duration_seconds=1.0,
                    net_measured_cost_delta=-2.5,
                    quality_delta_points=-1.5,
                )
            ]
        )
        self.assertEqual(-2.5, summary["case-01"]["net_measured_cost_delta"])
        self.assertEqual(-1.5, summary["case-01"]["quality_delta_points"])
        self.assertEqual(
            "COST_REDUCTION_SUPPORTED",
            workflow.evaluate_cost_claim(summary, minimum_cases=1),
        )

    def test_measured_usage_cannot_carry_projected_price(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "COST_EVIDENCE_INVALID"):
            workflow.normalize_cost_evidence(
                cost_record(
                    input_tokens=10,
                    duration_seconds=1.0,
                    evidence_class="measured",
                    projected_cost_usd=0.25,
                )
            )

    def test_every_accepted_price_field_requires_projection_and_rate_snapshot(self):
        for field in sorted(costs._PRICE_FIELDS):
            measured = cost_record(
                input_tokens=10,
                duration_seconds=1.0,
                evidence_class="measured",
                **{field: 0.25},
            )
            with self.subTest(field=field), self.assertRaisesRegex(
                workflow.WorkflowError, "COST_EVIDENCE_INVALID"
            ):
                workflow.normalize_cost_evidence(measured)

            projection = cost_record(
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                duration_seconds=None,
                evidence_class="sample_validated_projection",
                rate_snapshot_id="rates-2026-08-08",
                **{field: 0.25},
            )
            with self.subTest(field=f"projection:{field}"):
                self.assertEqual(
                    "sample_validated_projection",
                    workflow.normalize_cost_evidence(projection).evidence_class,
                )

    def test_paired_fixture_is_stable_stratified_and_has_both_surfaces(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "paired-cases.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(fixture["synthetic"])
        cases = fixture["cases"]
        self.assertEqual([f"case-{index:02d}" for index in range(1, 31)], [case["paired_case_id"] for case in cases])
        self.assertEqual({"direct", "sol_only", "delegated"}, {case["route"] for case in cases})
        surfaces = {
            attempt["execution_surface"]
            for case in cases
            for attempt in case["attempts"]
        }
        self.assertEqual(
            {"NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"},
            surfaces,
        )

    def test_synthetic_fixture_flows_through_aggregation_and_stays_out_of_metrics(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "paired-cases.json").read_text(
                encoding="utf-8"
            )
        )
        records = [
            attempt
            for case in fixture["cases"]
            for attempt in case["attempts"]
        ]
        summary = workflow.aggregate_paired_cases(records)
        self.assertEqual(30, len(summary))
        self.assertEqual("NO_COST_REDUCTION_PROVEN", workflow.evaluate_cost_claim(summary))
        metrics = {
            "calibration_task_count": 0,
            "experiment_task_count": 0,
            "role_calls": {},
            "cost_summary": {},
        }
        report = workflow.render_report(metrics)
        self.assertIn("paired-case count: 0", report)
        self.assertNotIn("case-01", report)


class CostReportTest(unittest.TestCase):
    def test_report_keeps_measured_projection_and_unavailable_separate(self):
        records = [
            cost_record(input_tokens=10, cached_input_tokens=0, output_tokens=2),
            cost_record(
                paired_case_id="case-02",
                evidence_class="sample_validated_projection",
                rate_snapshot_id="rates-2026-08-03",
                input_tokens=10,
                cached_input_tokens=0,
                output_tokens=2,
            ),
            cost_record(paired_case_id="case-03"),
        ]
        report = workflow.render_report(
            {
                "calibration_task_count": 0,
                "experiment_task_count": 0,
                "role_calls": {},
                "cost_summary": workflow.aggregate_paired_cases(records),
            }
        )
        self.assertIn("## Measured", report)
        self.assertIn("## Projection", report)
        self.assertIn("## Unavailable", report)
        self.assertIn("route", report)
        self.assertIn("execution surface", report)
        self.assertIn("retry overhead", report)
        self.assertIn("prompt bytes", report)
        self.assertIn("paired-case count", report)
        self.assertIn(
            "This calibration report proves only that the Luna read-only path can run",
            report,
        )

    def test_report_shows_signed_deltas_and_projection_amounts_without_mixing_classes(self):
        records = [
            cost_record(
                input_tokens=10,
                duration_seconds=1.0,
                net_measured_cost_delta=-2.5,
                quality_delta_points=-1.5,
            ),
            cost_record(
                paired_case_id="case-02",
                evidence_class="sample_validated_projection",
                rate_snapshot_id="rates-2026-08-03",
                duration_seconds=None,
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                projected_cost_usd=0.25,
            ),
            cost_record(paired_case_id="case-03"),
        ]
        report = workflow.render_report(
            {
                "calibration_task_count": 0,
                "experiment_task_count": 0,
                "role_calls": {},
                "cost_summary": workflow.aggregate_paired_cases(records),
            }
        )
        self.assertIn("net measured cost delta: -2.5", report)
        self.assertIn("quality delta points: -1.5", report)
        self.assertIn("measured input tokens: 10", report)
        self.assertIn("measured duration seconds: 1.0", report)
        self.assertIn("projected cost: 0.25", report)
        self.assertIn("rate snapshot: rates-2026-08-03", report)
        self.assertIn("unavailable attempts: 1", report)

    @staticmethod
    def _passing_summary(case_count):
        return {
            f"case-{index:02d}": {
                "net_measured_cost_delta": -1.0,
                "quality_delta_points": 0.0,
                "measured_attempt_count": 1,
            }
            for index in range(case_count)
        }

    def test_report_claim_gate_uses_pinned_minimum_cases(self):
        summary = self._passing_summary(8)
        metrics = {
            "calibration_task_count": 0,
            "experiment_task_count": 0,
            "role_calls": {},
            "cost_summary": summary,
        }
        pinned = workflow.render_report(metrics, claim_minimum_cases=8)
        expected = workflow.evaluate_cost_claim(
            {
                "paired_case_count": 8,
                "quality_delta_points": 0.0,
                "net_measured_cost_delta": -8.0,
            },
            minimum_cases=8,
        )
        self.assertEqual("COST_REDUCTION_SUPPORTED", expected)
        self.assertIn(f"- claim gate: {expected}", pinned)
        default = workflow.render_report(metrics)
        self.assertIn("- claim gate: OBSERVATION_ONLY", default)

    def test_report_prints_optimization_gate_line(self):
        summary = self._passing_summary(8)
        metrics = {
            "calibration_task_count": 0,
            "experiment_task_count": 0,
            "role_calls": {},
            "cost_summary": summary,
            "p0_miss_count": 0,
            "p1_miss_count": 0,
            "calibration_first_delivery_pass_rate": 0.5,
            "experiment_first_delivery_pass_rate": 0.6,
            "synthetic_cost_attempt_count": 0,
        }
        expected = workflow.evaluate_optimization_gate(
            {
                "cost_summary": summary,
                "p0_miss_count": 0,
                "p1_miss_count": 0,
                "calibration_first_delivery_pass_rate": 0.5,
                "experiment_first_delivery_pass_rate": 0.6,
                "synthetic": False,
            },
            minimum_cases=8,
        )
        self.assertEqual("ALLOW_ENFORCED", expected)
        report = workflow.render_report(metrics, claim_minimum_cases=8)
        self.assertIn(f"- optimization gate: {expected}", report)
        self.assertIn("- optimization gate: FALLBACK_FIXED", workflow.render_report(metrics))

    def test_report_optimization_gate_treats_synthetic_records_as_fallback(self):
        metrics = {
            "calibration_task_count": 0,
            "experiment_task_count": 0,
            "role_calls": {},
            "cost_summary": self._passing_summary(8),
            "p0_miss_count": 0,
            "p1_miss_count": 0,
            "calibration_first_delivery_pass_rate": 0.5,
            "experiment_first_delivery_pass_rate": 0.6,
            "synthetic_cost_attempt_count": 3,
        }
        report = workflow.render_report(metrics, claim_minimum_cases=8)
        self.assertIn("- optimization gate: FALLBACK_FIXED", report)


class RateSnapshotTest(unittest.TestCase):
    ARCHIVE_BYTES = b"<html>official pricing capture</html>\n"
    NOW_UTC = "2026-08-28T12:00:00Z"
    RETRIEVED_AT = "2026-08-28T00:00:00Z"

    def _digest(self, payload: bytes = ARCHIVE_BYTES) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _valid_sku(self, **overrides):
        sku = {
            "sku": "gpt-5.6-sol",
            "model": "gpt-5.6",
            "currency": "USD",
            "unit": "PER_1M_TOKENS",
            "billing_channel": "api",
            "price_uncached_input": "2.50",
            "price_cached_input": "0.25",
            "price_output": "10.00",
            "cache_write_applies": True,
            "long_context_tiers_applies": False,
            "source_url": "https://example.test/pricing",
            "retrieved_at": self.RETRIEVED_AT,
        }
        sku.update(overrides)
        return sku

    def _valid_archive(self, digest: str, **overrides):
        archive = {
            "archive_path": f"docs/rate-archives/{digest}",
            "archive_sha256": digest,
            "mime_type": "text/html",
            "retrieval_status": "retrieved",
        }
        archive.update(overrides)
        return archive

    def _valid_snapshot(self, digest: str | None = None, **overrides):
        digest = digest or self._digest()
        snapshot = {
            "schema_version": "ai-rate-snapshot-1",
            "rate_snapshot_id": "rates-2026-08-28",
            "skus": [self._valid_sku()],
            "effective_at": "2026-08-28T00:00:00Z",
            "retrieved_at": self.RETRIEVED_AT,
            "archive": self._valid_archive(digest),
            "approved_by": "owner",
            "approval_evidence_id": "a" * 64,
        }
        snapshot.update(overrides)
        return snapshot

    def _write_archive(self, root: Path, payload: bytes = ARCHIVE_BYTES) -> str:
        digest = self._digest(payload)
        path = root / "docs" / "rate-archives" / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return digest

    def test_closed_rate_units_and_field_sets(self):
        self.assertEqual("ai-rate-snapshot-1", costs.RATE_SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(
            frozenset(
                {
                    "schema_version",
                    "rate_snapshot_id",
                    "skus",
                    "effective_at",
                    "retrieved_at",
                    "archive",
                    "approved_by",
                    "approval_evidence_id",
                }
            ),
            costs.RATE_SNAPSHOT_FIELDS,
        )
        self.assertEqual(
            frozenset({"PER_TOKEN", "PER_1K_TOKENS", "PER_1M_TOKENS"}),
            costs.RATE_UNITS,
        )
        self.assertEqual(
            {"PER_TOKEN": 1, "PER_1K_TOKENS": 1_000, "PER_1M_TOKENS": 1_000_000},
            dict(costs.RATE_UNIT_BASE),
        )
        self.assertEqual(
            frozenset(
                {
                    "sku",
                    "model",
                    "currency",
                    "unit",
                    "billing_channel",
                    "price_uncached_input",
                    "price_cached_input",
                    "price_output",
                    "cache_write_applies",
                    "long_context_tiers_applies",
                    "source_url",
                    "retrieved_at",
                }
            ),
            costs.RATE_SNAPSHOT_SKU_FIELDS,
        )
        self.assertEqual(
            frozenset(
                {"archive_path", "archive_sha256", "mime_type", "retrieval_status"}
            ),
            costs.RATE_SNAPSHOT_ARCHIVE_FIELDS,
        )
        self.assertEqual(
            frozenset({"CURRENT", "PRICE_STALE", "PRICE_UNKNOWN"}),
            costs.PRICING_STATUSES,
        )

    def test_schema_declares_closed_units_and_required_fields(self):
        schema = json.loads(
            (ROOT / "config" / "ai_workflow_rate_snapshot.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual("ai-rate-snapshot-1", schema["properties"]["schema_version"]["const"])
        self.assertEqual(sorted(costs.RATE_SNAPSHOT_FIELDS), sorted(schema["required"]))
        sku = schema["properties"]["skus"]["items"]
        self.assertEqual(
            ["PER_TOKEN", "PER_1K_TOKENS", "PER_1M_TOKENS"],
            sku["properties"]["unit"]["enum"],
        )
        for field in ("price_uncached_input", "price_cached_input", "price_output"):
            self.assertEqual("string", sku["properties"][field]["type"])
        self.assertEqual(
            sorted(costs.RATE_SNAPSHOT_SKU_FIELDS), sorted(sku["required"])
        )
        archive = schema["properties"]["archive"]
        self.assertEqual(
            sorted(costs.RATE_SNAPSHOT_ARCHIVE_FIELDS), sorted(archive["required"])
        )

    def test_valid_snapshot_round_trips(self):
        snapshot = self._valid_snapshot()
        costs.validate_rate_snapshot(snapshot)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rates-2026-08-28.json"
            costs.write_rate_snapshot(path, snapshot)
            loaded = costs.load_rate_snapshot(path)
        self.assertEqual("ai-rate-snapshot-1", loaded["schema_version"])
        self.assertEqual(snapshot["rate_snapshot_id"], loaded["rate_snapshot_id"])
        self.assertEqual(snapshot["skus"], loaded["skus"])
        self.assertEqual(snapshot["archive"], loaded["archive"])
        self.assertEqual(costs.RATE_SNAPSHOT_FIELDS, set(loaded))

    def test_missing_sku_required_field_is_rejected(self):
        snapshot = self._valid_snapshot()
        del snapshot["skus"][0]["model"]
        with self.assertRaisesRegex(workflow.WorkflowError, "MISSING_FIELD"):
            costs.validate_rate_snapshot(snapshot)

    def test_sku_missing_source_url_is_rejected(self):
        snapshot = self._valid_snapshot()
        del snapshot["skus"][0]["source_url"]
        with self.assertRaisesRegex(workflow.WorkflowError, "MISSING_FIELD"):
            costs.validate_rate_snapshot(snapshot)

    def test_sku_unit_outside_rate_units_is_rejected(self):
        snapshot = self._valid_snapshot()
        snapshot["skus"][0]["unit"] = "PER_REQUEST"
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ENUM"):
            costs.validate_rate_snapshot(snapshot)

    def test_negative_and_non_decimal_prices_are_rejected(self):
        for field, value in (
            ("price_uncached_input", "-1.00"),
            ("price_cached_input", "not-a-price"),
            ("price_output", "1e-3"),
            ("price_uncached_input", 2.5),
        ):
            snapshot = self._valid_snapshot()
            snapshot["skus"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                workflow.WorkflowError, "INVALID_TYPE"
            ):
                costs.validate_rate_snapshot(snapshot)

    def test_non_utc_timestamps_are_rejected(self):
        for field, value in (
            ("effective_at", "2026-08-28T00:00:00+08:00"),
            ("retrieved_at", "2026-08-28 00:00:00"),
            ("effective_at", "2026-08-28T00:00:00"),
        ):
            snapshot = self._valid_snapshot()
            snapshot[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                workflow.WorkflowError, "INVALID_TYPE"
            ):
                costs.validate_rate_snapshot(snapshot)
        snapshot = self._valid_snapshot()
        snapshot["skus"][0]["retrieved_at"] = "2026-08-28T00:00:00+00:00"
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_TYPE"):
            costs.validate_rate_snapshot(snapshot)

    def test_calendar_invalid_utc_timestamps_fail_closed(self):
        for field, value in (
            ("effective_at", "2026-02-31T00:00:00Z"),
            ("retrieved_at", "2026-13-01T00:00:00Z"),
            ("effective_at", "2026-08-28T24:00:00Z"),
        ):
            snapshot = self._valid_snapshot()
            snapshot[field] = value
            with self.subTest(field=field, value=value):
                try:
                    costs.validate_rate_snapshot(snapshot)
                except workflow.WorkflowError as exc:
                    self.assertIn("INVALID_TYPE", str(exc))
                except ValueError:
                    self.fail("calendar-invalid timestamp leaked ValueError")
                else:
                    self.fail("expected WorkflowError for calendar-invalid timestamp")

    def test_missing_archive_or_approval_is_rejected(self):
        for field in ("archive", "approved_by", "approval_evidence_id"):
            snapshot = self._valid_snapshot()
            del snapshot[field]
            with self.subTest(field=field), self.assertRaisesRegex(
                workflow.WorkflowError, "MISSING_FIELD"
            ):
                costs.validate_rate_snapshot(snapshot)

    def test_stale_snapshot_is_price_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = self._write_archive(root)
            snapshot = self._valid_snapshot(digest)
            costs.validate_rate_snapshot(snapshot)
            status = costs.snapshot_pricing_status(
                snapshot,
                now_utc="2026-08-29T00:00:01Z",
                max_age_seconds=86400,
                root=root,
            )
        self.assertEqual("PRICE_STALE", status)
        self.assertIn(status, costs.PRICING_STATUSES)

    def test_missing_critical_price_fields_are_price_unknown(self):
        snapshot = self._valid_snapshot()
        del snapshot["skus"][0]["price_output"]
        status = costs.snapshot_pricing_status(
            snapshot,
            now_utc=self.NOW_UTC,
            max_age_seconds=86400,
        )
        self.assertEqual("PRICE_UNKNOWN", status)
        with self.assertRaisesRegex(workflow.WorkflowError, "MISSING_FIELD"):
            costs.validate_rate_snapshot(snapshot)

    def test_resolve_archive_returns_path_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = self._write_archive(root)
            snapshot = self._valid_snapshot(digest)
            resolved = costs.resolve_snapshot_archive(snapshot, root=root)
            self.assertEqual(root / "docs" / "rate-archives" / digest, resolved)
            self.assertEqual(self.ARCHIVE_BYTES, resolved.read_bytes())
            self.assertEqual(
                "CURRENT",
                costs.snapshot_pricing_status(
                    snapshot,
                    now_utc=self.NOW_UTC,
                    max_age_seconds=86400,
                    root=root,
                ),
            )

    def test_missing_or_mismatched_archive_is_unresolvable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = self._digest()
            snapshot = self._valid_snapshot(digest)
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RATE_ARCHIVE_UNRESOLVABLE"
            ):
                costs.resolve_snapshot_archive(snapshot, root=root)
            orphan = root / "docs" / "rate-archives" / digest
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(b"tampered pricing page\n")
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RATE_ARCHIVE_UNRESOLVABLE"
            ):
                costs.resolve_snapshot_archive(snapshot, root=root)

    def test_unresolvable_archive_is_price_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._valid_snapshot()
            status = costs.snapshot_pricing_status(
                snapshot,
                now_utc=self.NOW_UTC,
                max_age_seconds=86400,
                root=root,
            )
        self.assertEqual("PRICE_UNKNOWN", status)

    def test_omitted_archive_root_is_price_unknown(self):
        snapshot = self._valid_snapshot()
        omitted = costs.snapshot_pricing_status(
            snapshot,
            now_utc=self.NOW_UTC,
            max_age_seconds=86400,
        )
        explicit_none = costs.snapshot_pricing_status(
            snapshot,
            now_utc=self.NOW_UTC,
            max_age_seconds=86400,
            root=None,
        )
        self.assertEqual("PRICE_UNKNOWN", omitted)
        self.assertEqual("PRICE_UNKNOWN", explicit_none)
        self.assertNotEqual("CURRENT", omitted)

    def test_second_write_of_same_snapshot_id_is_rejected(self):
        snapshot = self._valid_snapshot()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / f"{snapshot['rate_snapshot_id']}.json"
            costs.write_rate_snapshot(path, snapshot)
            mutated = copy.deepcopy(snapshot)
            mutated["approved_by"] = "other-owner"
            mutated["skus"][0]["price_output"] = "99.00"
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RATE_SNAPSHOT_ALREADY_FROZEN"
            ):
                costs.write_rate_snapshot(path, mutated)
            loaded = costs.load_rate_snapshot(path)
        self.assertEqual("owner", loaded["approved_by"])
        self.assertEqual("10.00", loaded["skus"][0]["price_output"])

    def test_historical_cost_evidence_keeps_old_snapshot_id(self):
        evidence = workflow.normalize_cost_evidence(
            cost_record(
                evidence_class="sample_validated_projection",
                rate_snapshot_id="rates-2026-08-01",
                projected_cost_usd=0.25,
            )
        )
        self.assertEqual("rates-2026-08-01", evidence.rate_snapshot_id)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rates-2026-08-28.json"
            costs.write_rate_snapshot(path, self._valid_snapshot())
            loaded = costs.load_rate_snapshot(path)
        self.assertEqual("rates-2026-08-28", loaded["rate_snapshot_id"])
        self.assertEqual("rates-2026-08-01", evidence.rate_snapshot_id)
        self.assertEqual("rates-2026-08-01", evidence.to_dict()["rate_snapshot_id"])

    def test_optimization_gate_ignores_rate_snapshot(self):
        summary = {
            f"case-{index:02d}": {
                "net_measured_cost_delta": -1.0,
                "quality_delta_points": 0.0,
                "measured_attempt_count": 1,
            }
            for index in range(8)
        }
        metrics = {
            "cost_summary": summary,
            "p0_miss_count": 0,
            "p1_miss_count": 0,
            "calibration_first_delivery_pass_rate": 0.5,
            "experiment_first_delivery_pass_rate": 0.6,
            "synthetic": False,
        }
        without = costs.evaluate_optimization_gate(metrics, minimum_cases=8)
        with_snapshot = costs.evaluate_optimization_gate(
            {**metrics, "rate_snapshot": self._valid_snapshot()},
            minimum_cases=8,
        )
        self.assertEqual("ALLOW_ENFORCED", without)
        self.assertEqual(without, with_snapshot)
        self.assertEqual(
            without.__dict__ if hasattr(without, "__dict__") else without,
            with_snapshot.__dict__ if hasattr(with_snapshot, "__dict__") else with_snapshot,
        )


if __name__ == "__main__":
    unittest.main()
