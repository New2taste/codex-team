"""Regression baseline manifest checker: IDs, skip semantics, and shape."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "baseline_manifest.json"
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "base_commit",
        "captured_with",
        "captured_at_utc",
        "tests",
    }
)
ENTRY_FIELDS = frozenset({"id", "outcome", "skip_reason"})
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _discover_current_tests() -> dict[str, unittest.TestCase]:
    suite = unittest.TestLoader().discover(
        start_dir=str(ROOT / "tests"),
        pattern="test*.py",
    )
    return {test.id(): test for test in _flatten(suite)}


def _load_manifest() -> dict:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _static_skip(test: unittest.TestCase) -> tuple[bool, str]:
    method = getattr(test, test._testMethodName)
    if getattr(type(test), "__unittest_skip__", False):
        return True, getattr(type(test), "__unittest_skip_why__", "")
    if getattr(method, "__unittest_skip__", False):
        return True, getattr(method, "__unittest_skip_why__", "")
    return False, ""


class BaselineManifestTest(unittest.TestCase):
    def test_manifest_ids_all_present(self):
        manifest = _load_manifest()
        current_ids = set(_discover_current_tests())
        for entry in manifest["tests"]:
            self.assertIn(entry["id"], current_ids)

    def test_skip_semantics_unchanged(self):
        manifest = _load_manifest()
        current = _discover_current_tests()
        for entry in manifest["tests"]:
            test = current[entry["id"]]
            skipped, why = _static_skip(test)
            if entry["outcome"] == "skip":
                self.assertTrue(
                    skipped,
                    f"{entry['id']} is recorded as skip but is not statically skipped",
                )
                self.assertEqual(entry["skip_reason"], why)
            else:
                self.assertFalse(
                    skipped,
                    f"{entry['id']} is recorded as pass but is now statically skipped",
                )

    def test_manifest_shape(self):
        self.assertTrue(MANIFEST_PATH.is_file(), "tests/baseline_manifest.json is missing")
        manifest = _load_manifest()
        self.assertEqual(MANIFEST_FIELDS, set(manifest))
        self.assertEqual("ai-test-baseline-1", manifest["schema_version"])
        self.assertRegex(manifest["base_commit"], COMMIT_SHA)
        self.assertIsInstance(manifest["captured_with"], str)
        self.assertTrue(manifest["captured_with"])
        self.assertIsInstance(manifest["captured_at_utc"], str)
        self.assertTrue(manifest["captured_at_utc"])
        tests = manifest["tests"]
        self.assertIsInstance(tests, list)
        ids = [entry["id"] for entry in tests]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(len(ids), len(set(ids)))
        for entry in tests:
            self.assertEqual(ENTRY_FIELDS, set(entry))
            self.assertIn(entry["outcome"], {"pass", "skip"})
            self.assertIsInstance(entry["skip_reason"], str)
            if entry["outcome"] == "skip":
                self.assertTrue(entry["skip_reason"])
            else:
                self.assertEqual("", entry["skip_reason"])
