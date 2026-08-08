import json
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow


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
        "evidence_class": "unavailable",
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
            )
        )
        self.assertEqual("measured", evidence.evidence_class)

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


if __name__ == "__main__":
    unittest.main()
