"""Behavior checks for the cost-conscious default and its closed escalations."""

import unittest
from types import SimpleNamespace

from scripts import ai_workflow as workflow
from scripts import ai_workflow_repairs as repairs
from scripts import ai_workflow_scheduler as scheduler


class BalancedPolicyTest(unittest.TestCase):
    def replay(self, **overrides):
        values = dict(
            whole_project_final=True,
            owner_actor=repairs.ActorIdentity("owner", "terra_xhigh"),
            reviewer_identities={"acceptor"},
            repairer_identities={"SOL_MEDIUM_REPAIR": "fixer"},
        )
        return SimpleNamespace(**{**values, **overrides})

    def assignment(self, phase, role, identity="independent"):
        return SimpleNamespace(
            phase=phase, expected_actor=repairs.ActorIdentity(identity, role)
        )

    def test_removed_low_role_cannot_be_selected(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_SEQUENCE_INVALID"):
            repairs._v2_validate_phase_actor(
                self.replay(), self.assignment("SOL_MEDIUM_PEER_REVIEW", "astra_low_reviewer")
            )

    def test_normal_peer_can_use_medium_or_explicit_astra_but_not_the_fixer(self):
        for role in ("sol_medium_reviewer", "astra_medium_reviewer"):
            repairs._v2_validate_phase_actor(
                self.replay(), self.assignment("SOL_MEDIUM_PEER_REVIEW", role)
            )
            with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_SEQUENCE_INVALID"):
                repairs._v2_validate_phase_actor(
                    self.replay(), self.assignment("SOL_MEDIUM_PEER_REVIEW", role, "fixer")
                )

    def test_final_review_requires_sol_high_and_independent_identity(self):
        replay = self.replay(reviewer_identities=set())
        repairs._v2_validate_phase_actor(replay, self.assignment("REVIEW_1", "sol_reviewer"))
        for role, identity in (("sol_medium_reviewer", "independent"), ("sol_reviewer", "owner")):
            with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_SEQUENCE_INVALID"):
                repairs._v2_validate_phase_actor(replay, self.assignment("REVIEW_1", role, identity))

    def test_terminal_choice_is_closed_to_sol_high_or_astra_medium(self):
        for role in ("sol_reviewer", "sol_xhigh"):
            repairs._v2_validate_phase_actor(
                self.replay(), self.assignment("SOL_XHIGH_TERMINAL_REPAIR", role)
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_SEQUENCE_INVALID"):
            repairs._v2_validate_phase_actor(
                self.replay(), self.assignment("SOL_XHIGH_TERMINAL_REPAIR", "terra_xhigh")
            )

    def test_mixed_batch_total_limit_includes_existing_inflight_steps(self):
        plan = SimpleNamespace(tasks=(
            SimpleNamespace(id="write", owner_role="terra_xhigh"),
            SimpleNamespace(id="read-a", owner_role="luna"),
            SimpleNamespace(id="read-b", owner_role="luna"),
        ))
        self.assertEqual(("write", "read-a"), scheduler._select_ready(
            plan, ("write", "read-a", "read-b"), {}, 2, 1, 2
        ))
        self.assertEqual(("read-a",), scheduler._select_ready(
            plan, ("read-a", "read-b"), {"write": "terra_xhigh"}, 2, 1, 2
        ))


if __name__ == "__main__":
    unittest.main()
