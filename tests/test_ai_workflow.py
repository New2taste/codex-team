import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContractFilesTest(unittest.TestCase):
    def test_role_models_and_efforts_are_pinned(self):
        with (ROOT / "config/ai_workflow.toml").open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(config["version"], "ai-workflow-1")
        self.assertEqual(
            (config["roles"]["luna"]["model"], config["roles"]["luna"]["reasoning_effort"]),
            ("gpt-5.6-luna", "max"),
        )
        self.assertEqual(
            (config["roles"]["terra"]["model"], config["roles"]["terra"]["reasoning_effort"]),
            ("gpt-5.6-terra", "xhigh"),
        )
        self.assertFalse(config["policy"]["automatic_xhigh"])
        self.assertFalse(config["policy"]["automatic_merge"])
        self.assertFalse(config["policy"]["automatic_push"])

    def test_contract_versions_and_closed_sets_are_pinned(self):
        task_schema = json.loads((ROOT / "config/ai_workflow_task.schema.json").read_text())
        result_schema = json.loads((ROOT / "config/ai_workflow_result.schema.json").read_text())
        self.assertEqual(task_schema["properties"]["schema_version"]["const"], "ai-task-1")
        self.assertEqual(result_schema["properties"]["schema_version"]["const"], "ai-result-1")
        self.assertEqual(
            set(task_schema["properties"]["verification_level"]["enum"]),
            {"L0", "L1", "L2"},
        )


if __name__ == "__main__":
    unittest.main()
