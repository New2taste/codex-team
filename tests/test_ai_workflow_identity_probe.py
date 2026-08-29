"""Isolated identity-probe-1 contract: dual-key gate and per-call output cap."""

from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from scripts import ai_workflow_identity_probe as probe
from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ai_workflow_identity_probe.py"
SCHEMA = ROOT / "config" / "ai_workflow_identity_probe_manifest.schema.json"
ROUTER_SCHEMA = ROOT / "config" / "ai_workflow_router_probe_manifest.schema.json"
CONFIG = ROOT / "config" / "ai_workflow.toml"
CREATED_AT = "2026-08-28T00:00:00Z"
IDENTITY_FIELDS = (
    "model",
    "effort",
    "sandbox",
    "permission",
    "fork_state",
    "nested_state",
)
PROTOCOL_FIELDS = frozenset(
    {
        "protocol_version",
        "arm",
        "requested_launch_intent",
        "observed_runtime_identity",
        "model_text_output",
        "max_calls",
        "max_output_tokens",
        "max_output_tokens_per_call",
    }
)


def _build_kwargs(**overrides):
    payload = {
        "batch_id": "identity-batch-1",
        "arm": "NO_OP",
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "seed": 1,
        "max_calls": 3,
        "max_output_tokens": 100,
        "max_output_tokens_per_call": 40,
        "created_at_utc": CREATED_AT,
    }
    payload.update(overrides)
    return payload


def _manifest(**overrides):
    return probe.build_identity_probe_manifest(**_build_kwargs(**overrides))


class _CountingExecutor:
    def __init__(self):
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        return payload


class IdentityProbeManifestContractTest(unittest.TestCase):
    def test_constants_lock_protocol_arms_kinds_sources_and_budget_fields(self):
        self.assertEqual(
            "identity-probe-manifest-1",
            probe.IDENTITY_PROBE_MANIFEST_SCHEMA_VERSION,
        )
        self.assertEqual("identity-probe-1", probe.IDENTITY_PROBE_PROTOCOL_VERSION)
        self.assertEqual(
            frozenset({"NO_OP", "ONE_TURN", "TWO_TURN"}),
            probe.IDENTITY_PROBE_ARMS,
        )
        self.assertEqual(
            frozenset({"DRY_RUN", "FAKE", "LIVE"}),
            probe.EXECUTOR_KINDS,
        )
        self.assertEqual(
            frozenset({"SERVER_METADATA", "RUNTIME_EVIDENCE"}),
            probe.IDENTITY_FIELD_SOURCES,
        )
        self.assertEqual(
            ("max_calls", "max_output_tokens", "max_output_tokens_per_call"),
            probe.IDENTITY_PROBE_BUDGET_FIELDS,
        )

    def test_schema_closes_protocol_budget_and_identity_sections(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        properties = schema["properties"]
        self.assertEqual(
            "identity-probe-manifest-1",
            properties["schema_version"]["const"],
        )
        self.assertEqual("identity-probe-1", properties["protocol_version"]["const"])
        self.assertEqual(
            ["NO_OP", "ONE_TURN", "TWO_TURN"],
            properties["arm"]["enum"],
        )
        self.assertEqual({"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}, set(
            properties["requested_launch_intent"]["properties"]["model"]["enum"]
        ))
        self.assertEqual(
            {"max", "medium", "xhigh"},
            set(properties["requested_launch_intent"]["properties"]["effort"]["enum"]),
        )
        for field in probe.IDENTITY_PROBE_BUDGET_FIELDS:
            self.assertEqual("integer", properties[field]["type"])
            self.assertEqual(1, properties[field]["minimum"])
            self.assertIn(field, schema["required"])
        observed = properties["observed_runtime_identity"]
        self.assertFalse(observed["additionalProperties"])
        self.assertEqual(
            ["SERVER_METADATA", "RUNTIME_EVIDENCE"],
            observed["properties"]["identity_source"]["enum"],
        )
        self.assertNotIn("MODEL_TEXT", observed["properties"]["identity_source"]["enum"])
        self.assertEqual(PROTOCOL_FIELDS | {"schema_version", "batch_id", "seed", "created_at_utc"}, set(properties))
        self.assertEqual(set(properties), set(schema["required"]))

    def test_valid_manifest_round_trips_through_builder_and_validator(self):
        built = _manifest()
        self.assertIsNone(probe.validate_identity_probe_manifest(built))
        self.assertEqual("identity-probe-manifest-1", built["schema_version"])
        self.assertEqual("identity-probe-1", built["protocol_version"])
        self.assertEqual("NO_OP", built["arm"])
        self.assertEqual(3, built["max_calls"])
        self.assertEqual(100, built["max_output_tokens"])
        self.assertEqual(40, built["max_output_tokens_per_call"])
        requested = built["requested_launch_intent"]
        self.assertEqual("gpt-5.6-sol", requested["model"])
        self.assertEqual("medium", requested["effort"])
        observed = built["observed_runtime_identity"]
        self.assertIn(observed["identity_source"], probe.IDENTITY_FIELD_SOURCES)
        for field in IDENTITY_FIELDS:
            self.assertEqual("AUTHORITY_UNAVAILABLE", observed[field])
        self.assertEqual("", built["model_text_output"])
        loaded = json.loads(json.dumps(built))
        self.assertIsNone(probe.validate_identity_probe_manifest(loaded))
        self.assertEqual(built, loaded)

    def test_validator_rejects_unknown_arm_model_effort_protocol_and_extra_fields(self):
        cases = (
            ("unknown arm", {"arm": "THREE_TURN"}),
            ("unknown model", {"model": "gpt-5.6-gpt"}),
            ("unknown effort", {"effort": "high"}),
        )
        for label, overrides in cases:
            with self.subTest(label=label):
                with self.assertRaises(probe.IdentityProbeError):
                    probe.build_identity_probe_manifest(**_build_kwargs(**overrides))

        mutations = []
        extra = _manifest()
        extra["winner"] = "sol"
        mutations.append(("extra field", extra))
        protocol = _manifest()
        protocol["protocol_version"] = "router-probe-v1"
        mutations.append(("wrong protocol", protocol))
        schema = _manifest()
        schema["schema_version"] = "router-probe-manifest-1"
        mutations.append(("wrong schema", schema))
        for label, value in mutations:
            with self.subTest(label=label):
                with self.assertRaises(probe.IdentityProbeError):
                    probe.validate_identity_probe_manifest(value)

    def test_validator_rejects_missing_or_non_positive_budget_fields(self):
        for field in probe.IDENTITY_PROBE_BUDGET_FIELDS:
            missing = _manifest()
            del missing[field]
            with self.subTest(missing=field):
                with self.assertRaises(probe.IdentityProbeError):
                    probe.validate_identity_probe_manifest(missing)
            for invalid in (0, -1, True, False, 1.5, "1", None):
                mutated = _manifest()
                mutated[field] = invalid
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(probe.IdentityProbeError):
                        probe.validate_identity_probe_manifest(mutated)
            with self.subTest(builder=field):
                with self.assertRaises(probe.IdentityProbeError):
                    probe.build_identity_probe_manifest(
                        **_build_kwargs(**{field: 0})
                    )

    def test_validator_rejects_model_text_identity_source(self):
        for source in ("MODEL_TEXT", "SELF_REPORTED", "NL_HANDSHAKE"):
            mutated = _manifest()
            mutated["observed_runtime_identity"] = dict(
                mutated["observed_runtime_identity"]
            )
            mutated["observed_runtime_identity"]["identity_source"] = source
            with self.subTest(source=source):
                with self.assertRaises(probe.IdentityProbeError):
                    probe.validate_identity_probe_manifest(mutated)


class IdentityProbeDualKeyTest(unittest.TestCase):
    def test_config_defaults_identity_probe_enabled_to_false(self):
        loaded = probe.load_identity_probe_config(CONFIG)
        self.assertFalse(loaded["enabled"])
        document = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertNotIn("identity_probe", document)

    def test_live_requires_both_keys_before_any_executor_call(self):
        executor = _CountingExecutor()
        denied = (
            ({"identity_probe": {"enabled": False}}, True),
            ({"enabled": False}, True),
            ({"identity_probe": {"enabled": True}}, False),
            ({"enabled": True}, False),
        )
        for config, allow_live_model in denied:
            with self.subTest(config=config, allow_live_model=allow_live_model):
                with self.assertRaises(probe.IdentityProbeError) as ctx:
                    probe.require_identity_probe_authorized(
                        config,
                        allow_live_model=allow_live_model,
                        executor_kind="LIVE",
                    )
                self.assertEqual(
                    "IDENTITY_PROBE_NOT_AUTHORIZED",
                    ctx.exception.code,
                )
                with self.assertRaises(probe.IdentityProbeError) as run_ctx:
                    probe.run_identity_probe(
                        _manifest(),
                        config=config,
                        allow_live_model=allow_live_model,
                        executor=executor,
                        executor_kind="LIVE",
                        experiment_root=Path("/tmp/identity-probe-unused"),
                    )
                self.assertEqual(
                    "IDENTITY_PROBE_NOT_AUTHORIZED",
                    run_ctx.exception.code,
                )
                self.assertEqual(0, executor.calls)

    def test_dual_keys_admit_live_gate_without_calling_executor(self):
        probe.require_identity_probe_authorized(
            {"identity_probe": {"enabled": True}},
            allow_live_model=True,
            executor_kind="LIVE",
        )
        probe.require_identity_probe_authorized(
            {"enabled": True},
            allow_live_model=True,
            executor_kind="LIVE",
        )

    def test_dry_run_and_fake_do_not_need_keys(self):
        for kind in ("DRY_RUN", "FAKE"):
            with self.subTest(kind=kind):
                probe.require_identity_probe_authorized(
                    {"identity_probe": {"enabled": False}},
                    allow_live_model=False,
                    executor_kind=kind,
                )

    def test_unknown_executor_kind_is_rejected_without_calling_executor(self):
        executor = _CountingExecutor()
        with self.assertRaises(probe.IdentityProbeError):
            probe.require_identity_probe_authorized(
                {"identity_probe": {"enabled": True}},
                allow_live_model=True,
                executor_kind="PROD",
            )
        with self.assertRaises(probe.IdentityProbeError):
            probe.run_identity_probe(
                _manifest(),
                config={"identity_probe": {"enabled": True}},
                allow_live_model=True,
                executor=executor,
                executor_kind="PROD",
                experiment_root=Path("/tmp/identity-probe-unused"),
            )
        self.assertEqual(0, executor.calls)

    def test_run_identity_probe_is_the_only_public_executor_entry(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
        public_with_executor = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            names = {arg.arg for arg in node.args.args}
            names.update(arg.arg for arg in node.args.kwonlyargs)
            if "executor" in names:
                public_with_executor.append(node.name)
        self.assertEqual(["run_identity_probe"], public_with_executor)
        parameters = inspect.signature(probe.run_identity_probe).parameters
        self.assertIn("config", parameters)
        self.assertIn("allow_live_model", parameters)
        self.assertIn("executor", parameters)
        self.assertIn("executor_kind", parameters)
        source = inspect.getsource(probe.run_identity_probe)
        func = ast.parse(source).body[0]
        body = [
            stmt
            for stmt in func.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
            )
        ]
        first = body[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertIsInstance(first.value, ast.Call)
        called = first.value.func
        if isinstance(called, ast.Name):
            self.assertEqual("require_identity_probe_authorized", called.id)
        else:
            self.assertEqual("require_identity_probe_authorized", called.attr)


class IdentityProbeIdentitySourceTest(unittest.TestCase):
    def test_model_text_cannot_fill_identity_fields(self):
        claimed = {
            "model": "gpt-5.6-luna",
            "effort": "max",
            "sandbox": "workspace-write",
            "permission": "full",
            "fork_state": "VERIFIED_NONE",
            "nested_state": "VERIFIED_NONE",
        }
        manifest = _manifest(model="gpt-5.6-sol", effort="medium")
        polluted = copy.deepcopy(dict(manifest))
        polluted["model_text_output"] = json.dumps(claimed)
        probe.validate_identity_probe_manifest(polluted)
        observed = polluted["observed_runtime_identity"]
        requested = polluted["requested_launch_intent"]
        for field in IDENTITY_FIELDS:
            self.assertEqual("AUTHORITY_UNAVAILABLE", observed[field])
            self.assertNotEqual(claimed[field], observed[field])
            self.assertNotIn(str(claimed[field]), json.dumps(observed))
        self.assertEqual("gpt-5.6-sol", requested["model"])
        self.assertEqual("medium", requested["effort"])
        self.assertNotEqual(claimed["model"], requested["model"])
        self.assertNotEqual(claimed["effort"], requested["effort"])
        self.assertEqual(json.dumps(claimed), polluted["model_text_output"])
        self.assertNotIn("model_text_output", observed)
        text = polluted["model_text_output"]
        for field in IDENTITY_FIELDS:
            self.assertIn(f'"{field}"', text)

    def test_missing_authority_stays_unavailable_and_is_not_self_reported(self):
        observed = _manifest()["observed_runtime_identity"]
        for field in IDENTITY_FIELDS:
            self.assertEqual("AUTHORITY_UNAVAILABLE", observed[field])
        self.assertNotEqual("MODEL_TEXT", observed["identity_source"])
        self.assertIn(observed["identity_source"], probe.IDENTITY_FIELD_SOURCES)


class IdentityProbeIsolationTest(unittest.TestCase):
    def test_module_does_not_import_production_store_or_repairs(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
        self.assertNotIn("ai_workflow", imported)
        self.assertNotIn("ai_workflow_repairs", imported)
        self.assertNotIn("ai_workflow_router_probe", imported)

    def test_identity_probe_protocol_is_disjoint_from_router_probe(self):
        identity = json.loads(SCHEMA.read_text(encoding="utf-8"))
        router = json.loads(ROUTER_SCHEMA.read_text(encoding="utf-8"))
        self.assertNotEqual(
            identity["properties"]["schema_version"]["const"],
            router["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            "identity-probe-1",
            identity["properties"]["protocol_version"]["const"],
        )
        self.assertNotIn("protocol_version", router["properties"])
        self.assertTrue(PROTOCOL_FIELDS <= set(identity["properties"]))
        self.assertFalse(PROTOCOL_FIELDS & set(router["properties"]))
        self.assertNotIn("cases", identity["properties"])
        self.assertNotIn("arms", identity["properties"])
        self.assertNotIn("prompt_template_version", identity["properties"])

    def test_schema_is_distributed_and_script_is_not_runtime(self):
        self.assertIn(
            "ai_workflow_identity_probe_manifest.schema.json",
            sync_plugin.CONFIG_FILES,
        )
        self.assertNotIn("ai_workflow_identity_probe.py", sync_plugin.RUNTIME_FILES)


class IdentityProbeDryRunTest(unittest.TestCase):
    def test_dry_run_prints_manifest_summary_and_exits_zero(self):
        manifest = _manifest(arm="ONE_TURN")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = probe.main(["dry-run", "--manifest", str(path)])
        self.assertEqual(0, code)
        output = buffer.getvalue()
        self.assertIn("identity-probe-1", output)
        self.assertIn("ONE_TURN", output)
        self.assertIn("max_output_tokens_per_call", output)
        self.assertIn("40", output)

    def test_load_identity_probe_config_reads_enabled_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ai_workflow.toml"
            path.write_text(
                "[identity_probe]\nenabled = true\n",
                encoding="utf-8",
            )
            loaded = probe.load_identity_probe_config(path)
        self.assertTrue(loaded["enabled"])


if __name__ == "__main__":
    unittest.main()
