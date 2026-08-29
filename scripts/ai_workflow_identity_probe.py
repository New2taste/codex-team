"""Isolated identity-probe-1 contract, dual-key gate, and dry-run CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path


class IdentityProbeError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        self.code = code
        self.message = str(message)
        super().__init__(f"{code}: {message}" if code else str(message))


IDENTITY_PROBE_MANIFEST_SCHEMA_VERSION = "identity-probe-manifest-1"
IDENTITY_PROBE_PROTOCOL_VERSION = "identity-probe-1"
IDENTITY_PROBE_ARMS = frozenset({"NO_OP", "ONE_TURN", "TWO_TURN"})
EXECUTOR_KINDS = frozenset({"DRY_RUN", "FAKE", "LIVE"})
IDENTITY_FIELD_SOURCES = frozenset({"SERVER_METADATA", "RUNTIME_EVIDENCE"})
IDENTITY_PROBE_BUDGET_FIELDS = (
    "max_calls",
    "max_output_tokens",
    "max_output_tokens_per_call",
)
IDENTITY_PROBE_MODELS = frozenset({"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"})
IDENTITY_PROBE_EFFORTS = frozenset({"max", "medium", "xhigh"})
AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "batch_id",
        "arm",
        "seed",
        "max_calls",
        "max_output_tokens",
        "max_output_tokens_per_call",
        "created_at_utc",
        "requested_launch_intent",
        "observed_runtime_identity",
        "model_text_output",
    }
)
_REQUESTED_FIELDS = frozenset({"model", "effort"})
_OBSERVED_FIELDS = frozenset(
    {
        "identity_source",
        "model",
        "effort",
        "sandbox",
        "permission",
        "fork_state",
        "nested_state",
    }
)
_IDENTITY_FIELDS = (
    "model",
    "effort",
    "sandbox",
    "permission",
    "fork_state",
    "nested_state",
)


def _fail(message: str, code: str | None = None) -> None:
    raise IdentityProbeError(message, code=code)


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        _fail(f"{field} is invalid")
    return value


def _exact_fields(value: object, expected: frozenset[str], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(f"{field} shape is invalid")
    return dict(value)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{field} must be a positive integer")
    return value


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        _fail("created_at_utc is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("created_at_utc is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("created_at_utc must be UTC")
    return value


def _validate_requested_launch_intent(value: object) -> dict[str, object]:
    requested = _exact_fields(value, _REQUESTED_FIELDS, "requested_launch_intent")
    model = requested["model"]
    effort = requested["effort"]
    if not isinstance(model, str) or model not in IDENTITY_PROBE_MODELS:
        _fail("requested_launch_intent.model is invalid")
    if not isinstance(effort, str) or effort not in IDENTITY_PROBE_EFFORTS:
        _fail("requested_launch_intent.effort is invalid")
    return {"model": model, "effort": effort}


def _validate_observed_runtime_identity(value: object) -> dict[str, object]:
    observed = _exact_fields(value, _OBSERVED_FIELDS, "observed_runtime_identity")
    source = observed["identity_source"]
    if not isinstance(source, str) or source not in IDENTITY_FIELD_SOURCES:
        _fail("observed_runtime_identity.identity_source is invalid")
    normalized = {"identity_source": source}
    for field in _IDENTITY_FIELDS:
        item = observed[field]
        if not isinstance(item, str) or not item:
            _fail(f"observed_runtime_identity.{field} is invalid")
        normalized[field] = item
    return normalized


def validate_identity_probe_manifest(value: Mapping[str, object]) -> None:
    manifest = _exact_fields(value, _TOP_FIELDS, "manifest")
    if manifest["schema_version"] != IDENTITY_PROBE_MANIFEST_SCHEMA_VERSION:
        _fail("schema_version is invalid")
    if manifest["protocol_version"] != IDENTITY_PROBE_PROTOCOL_VERSION:
        _fail("protocol_version is invalid")
    _safe_id(manifest["batch_id"], "batch_id")
    if manifest["arm"] not in IDENTITY_PROBE_ARMS:
        _fail("arm is invalid")
    seed = manifest["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        _fail("seed is invalid")
    for field in IDENTITY_PROBE_BUDGET_FIELDS:
        _positive_int(manifest[field], field)
    _validate_timestamp(manifest["created_at_utc"])
    _validate_requested_launch_intent(manifest["requested_launch_intent"])
    _validate_observed_runtime_identity(manifest["observed_runtime_identity"])
    text = manifest["model_text_output"]
    if not isinstance(text, str):
        _fail("model_text_output is invalid")


def build_identity_probe_manifest(
    *,
    batch_id: str,
    arm: str,
    model: str,
    effort: str,
    seed: int,
    max_calls: int,
    max_output_tokens: int,
    max_output_tokens_per_call: int,
    created_at_utc: str,
) -> Mapping[str, object]:
    if arm not in IDENTITY_PROBE_ARMS:
        _fail("arm is invalid")
    if model not in IDENTITY_PROBE_MODELS:
        _fail("model is invalid")
    if effort not in IDENTITY_PROBE_EFFORTS:
        _fail("effort is invalid")
    manifest = {
        "schema_version": IDENTITY_PROBE_MANIFEST_SCHEMA_VERSION,
        "protocol_version": IDENTITY_PROBE_PROTOCOL_VERSION,
        "batch_id": batch_id,
        "arm": arm,
        "seed": seed,
        "max_calls": max_calls,
        "max_output_tokens": max_output_tokens,
        "max_output_tokens_per_call": max_output_tokens_per_call,
        "created_at_utc": created_at_utc,
        "requested_launch_intent": {"model": model, "effort": effort},
        "observed_runtime_identity": {
            "identity_source": "SERVER_METADATA",
            **{field: AUTHORITY_UNAVAILABLE for field in _IDENTITY_FIELDS},
        },
        "model_text_output": "",
    }
    validate_identity_probe_manifest(manifest)
    return manifest


def load_identity_probe_config(path: Path) -> Mapping[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("identity probe configuration must be a regular file")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise IdentityProbeError(
            "identity probe configuration cannot be read"
        ) from exc
    raw = document.get("identity_probe") if isinstance(document, Mapping) else None
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, Mapping):
        _fail("identity_probe configuration is invalid")
    if "enabled" not in raw:
        return {"enabled": False}
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        _fail("identity_probe.enabled is invalid")
    return {"enabled": enabled}


def _identity_probe_enabled(config: Mapping[str, object]) -> bool:
    section = config.get("identity_probe")
    if isinstance(section, Mapping):
        return section.get("enabled") is True
    return config.get("enabled") is True


def require_identity_probe_authorized(
    config: Mapping[str, object],
    *,
    allow_live_model: bool,
    executor_kind: str,
) -> None:
    if executor_kind not in EXECUTOR_KINDS:
        _fail("executor_kind is invalid")
    if executor_kind != "LIVE":
        return
    if not _identity_probe_enabled(config) or allow_live_model is not True:
        _fail(
            "live identity probe requires identity_probe.enabled and --allow-live-model",
            code="IDENTITY_PROBE_NOT_AUTHORIZED",
        )


def run_identity_probe(
    manifest: Mapping[str, object],
    *,
    config: Mapping[str, object],
    allow_live_model: bool,
    executor: Callable[[Mapping[str, object]], Mapping[str, object]],
    executor_kind: str,
    experiment_root: Path,
) -> list[Mapping[str, object]]:
    require_identity_probe_authorized(
        config,
        allow_live_model=allow_live_model,
        executor_kind=executor_kind,
    )
    raise IdentityProbeError(
        "identity probe runner is not implemented",
        code="IDENTITY_PROBE_RUNNER_UNIMPLEMENTED",
    )


def _load_manifest(path: Path) -> dict[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("manifest path must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityProbeError("manifest cannot be read") from exc
    if not isinstance(value, Mapping):
        _fail("manifest shape is invalid")
    validate_identity_probe_manifest(value)
    return dict(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-workflow-identity-probe")
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
        requested = manifest["requested_launch_intent"]
        print(
            json.dumps(
                {
                    "protocol_version": manifest["protocol_version"],
                    "schema_version": manifest["schema_version"],
                    "batch_id": manifest["batch_id"],
                    "arm": manifest["arm"],
                    "requested_launch_intent": requested,
                    "observed_runtime_identity": manifest["observed_runtime_identity"],
                    "max_calls": manifest["max_calls"],
                    "max_output_tokens": manifest["max_output_tokens"],
                    "max_output_tokens_per_call": manifest[
                        "max_output_tokens_per_call"
                    ],
                    "live_model_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except IdentityProbeError as exc:
        print(f"IDENTITY_PROBE_FAILED: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
