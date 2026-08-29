"""Isolated identity-probe-1 contract, dual-key gate, and dry-run CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
IDENTITY_PROBE_USAGE_FIELDS = (
    "uncached_input_tokens",
    "cached_input_tokens",
    "output_tokens",
)
_ARM_ORDER = ("NO_OP", "ONE_TURN", "TWO_TURN")
_PAIRED_DELTAS = (
    ("ONE_TURN_MINUS_NO_OP", "ONE_TURN", "NO_OP"),
    ("TWO_TURN_MINUS_ONE_TURN", "TWO_TURN", "ONE_TURN"),
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


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _arm_config_hash(manifest: Mapping[str, object]) -> str:
    payload = {
        "arm": manifest["arm"],
        "requested_launch_intent": dict(manifest["requested_launch_intent"]),
        "seed": manifest["seed"],
        "max_calls": manifest["max_calls"],
        "max_output_tokens": manifest["max_output_tokens"],
        "max_output_tokens_per_call": manifest["max_output_tokens_per_call"],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _unavailable_identity() -> dict[str, object]:
    return {
        "identity_source": "SERVER_METADATA",
        **{field: AUTHORITY_UNAVAILABLE for field in _IDENTITY_FIELDS},
    }


def _usage_from_executor(returned: object) -> dict[str, int] | None:
    if not isinstance(returned, Mapping):
        return None
    usage: dict[str, int] = {}
    for field in IDENTITY_PROBE_USAGE_FIELDS:
        item = returned.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        usage[field] = item
    return usage


def _identity_from_executor(returned: Mapping[str, object]) -> dict[str, object]:
    raw = returned.get("observed_runtime_identity")
    if not isinstance(raw, Mapping):
        return _unavailable_identity()
    try:
        return _validate_observed_runtime_identity(raw)
    except IdentityProbeError:
        return _unavailable_identity()


def _percentile(values: list[int], percent: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (percent / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    weight = rank - low
    return float(ordered[low]) * (1.0 - weight) + float(ordered[high]) * weight


def _usage_stats(values: list[int]) -> dict[str, object]:
    return {
        "total": sum(values),
        "mean": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "p50": _percentile(values, 50),
        "p90": _percentile(values, 90),
    }


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, row: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def aggregate_identity_probe_results(
    records: list[Mapping[str, object]],
) -> Mapping[str, object]:
    by_arm: dict[str, list[Mapping[str, object]]] = {arm: [] for arm in _ARM_ORDER}
    failures = {arm: 0 for arm in _ARM_ORDER}
    hashes: dict[str, str] = {}
    experiment_root: str | None = None
    per_arm_calls: dict[str, int] = {}
    per_arm_tokens: dict[str, int] = {}
    per_arm_stop: dict[str, str] = {}
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        arm = raw.get("arm")
        if arm not in by_arm:
            continue
        arm_name = str(arm)
        root = raw.get("experiment_root")
        if experiment_root is None and isinstance(root, str):
            experiment_root = root
        reason = raw.get("stop_reason")
        if isinstance(reason, str) and reason:
            per_arm_stop[arm_name] = reason
        used = raw.get("tokens_used")
        if not isinstance(used, bool) and isinstance(used, int):
            per_arm_tokens[arm_name] = used
        made = raw.get("calls_made")
        if not isinstance(made, bool) and isinstance(made, int):
            per_arm_calls[arm_name] = made
        digest = raw.get("arm_config_hash")
        if isinstance(digest, str) and digest:
            hashes[arm_name] = digest
        if raw.get("record_valid") is True:
            by_arm[arm_name].append(raw)
        else:
            failures[arm_name] += 1
    calls_made = sum(per_arm_calls.get(arm, 0) for arm in _ARM_ORDER)
    tokens_used = sum(per_arm_tokens.get(arm, 0) for arm in _ARM_ORDER)
    ordered_reasons = [per_arm_stop[arm] for arm in _ARM_ORDER if arm in per_arm_stop]
    unique_reasons = list(dict.fromkeys(ordered_reasons))
    if len(unique_reasons) == 1:
        stop_reason: str | dict[str, str] | None = unique_reasons[0]
    elif unique_reasons:
        stop_reason = {
            arm: per_arm_stop[arm] for arm in _ARM_ORDER if arm in per_arm_stop
        }
    else:
        stop_reason = None
    summary: dict[str, object] = {
        "protocol_version": IDENTITY_PROBE_PROTOCOL_VERSION,
        "experiment_root": experiment_root,
        "calls_made": calls_made,
        "tokens_used": tokens_used,
        "stop_reason": stop_reason,
    }
    for arm in _ARM_ORDER:
        samples = by_arm[arm]
        group: dict[str, object] = {
            "sample_count": len(samples),
            "failure_count": failures[arm],
            "arm_config_hash": hashes.get(arm),
        }
        if not samples:
            group["status"] = "OBSERVATION_ONLY"
            summary[arm] = group
            continue
        group["status"] = "MEASURED"
        for field in IDENTITY_PROBE_USAGE_FIELDS:
            values = [int(item[field]) for item in samples]
            group[field] = _usage_stats(values)
        cached_total = int(group["cached_input_tokens"]["total"])
        uncached_total = int(group["uncached_input_tokens"]["total"])
        input_total = cached_total + uncached_total
        group["cache_hit_ratio"] = (
            cached_total / input_total if input_total else 0.0
        )
        summary[arm] = group
    deltas: dict[str, object] = {}
    for name, left, right in _PAIRED_DELTAS:
        left_group = summary[left]
        right_group = summary[right]
        if (
            not isinstance(left_group, Mapping)
            or not isinstance(right_group, Mapping)
            or left_group.get("status") == "OBSERVATION_ONLY"
            or right_group.get("status") == "OBSERVATION_ONLY"
        ):
            deltas[name] = "OBSERVATION_ONLY"
            continue
        deltas[name] = {
            field: left_group[field]["mean"] - right_group[field]["mean"]
            for field in IDENTITY_PROBE_USAGE_FIELDS
        }
    summary["paired_deltas"] = deltas
    return summary


def render_identity_probe_report(summary: Mapping[str, object]) -> str:
    lines = [
        "IDENTITY_PROBE_REPORT",
        f"protocol_version={summary.get('protocol_version')}",
        f"experiment_root={summary.get('experiment_root')}",
        f"calls_made={summary.get('calls_made')}",
        f"tokens_used={summary.get('tokens_used')}",
        f"stop_reason={summary.get('stop_reason')}",
    ]
    for arm in _ARM_ORDER:
        group = summary.get(arm)
        if not isinstance(group, Mapping):
            continue
        lines.append(
            f"{arm}: status={group.get('status')} "
            f"sample_count={group.get('sample_count')} "
            f"failure_count={group.get('failure_count')} "
            f"cache_hit_ratio={group.get('cache_hit_ratio')}"
        )
    return "\n".join(lines) + "\n"


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
    validate_identity_probe_manifest(manifest)
    experiment_root = Path(experiment_root)
    if experiment_root.exists() and (
        experiment_root.is_symlink() or not experiment_root.is_dir()
    ):
        _fail("experiment_root must be a directory")
    experiment_root.mkdir(parents=True, exist_ok=True)
    max_calls = int(manifest["max_calls"])
    max_output_tokens = int(manifest["max_output_tokens"])
    max_output_tokens_per_call = int(manifest["max_output_tokens_per_call"])
    arm_dir = experiment_root / str(manifest["batch_id"]) / str(manifest["arm"])
    arm_dir.mkdir(parents=True, exist_ok=True)
    _write_json(arm_dir / "manifest.json", dict(manifest))
    jsonl_path = arm_dir / "records.jsonl"
    arm_hash = _arm_config_hash(manifest)
    records: list[dict[str, object]] = []
    calls_made = 0
    tokens_used = 0
    stop_reason = "MAX_CALLS"
    while True:
        if calls_made >= max_calls:
            stop_reason = "MAX_CALLS"
            break
        if tokens_used + max_output_tokens_per_call >= max_output_tokens:
            stop_reason = "MAX_OUTPUT_TOKENS"
            break
        payload = {
            "arm": manifest["arm"],
            "call_index": calls_made,
            "protocol_version": IDENTITY_PROBE_PROTOCOL_VERSION,
            "requested_launch_intent": dict(manifest["requested_launch_intent"]),
        }
        returned = executor(payload)
        calls_made += 1
        if not isinstance(returned, Mapping):
            returned = {}
        usage = _usage_from_executor(returned)
        model_text = returned.get("model_text_output")
        if not isinstance(model_text, str):
            model_text = ""
        observed = _identity_from_executor(returned)
        record_valid = usage is not None
        per_call_status = "OK"
        if record_valid and usage["output_tokens"] > max_output_tokens_per_call:
            per_call_status = "PER_CALL_CAP_EXCEEDED"
            stop_reason = "PER_CALL_CAP_EXCEEDED"
        if record_valid:
            tokens_used += usage["output_tokens"]
            cache_status = returned.get("cache_status")
            if not isinstance(cache_status, str) or not cache_status:
                cache_status = (
                    "HIT" if usage["cached_input_tokens"] > 0 else "MISS"
                )
        else:
            cache_status = returned.get("cache_status")
            if not isinstance(cache_status, str) or not cache_status:
                cache_status = "UNAVAILABLE"
        runtime_metadata = returned.get("runtime_metadata")
        if not isinstance(runtime_metadata, Mapping):
            runtime_metadata = {"executor_kind": executor_kind}
        else:
            runtime_metadata = dict(runtime_metadata)
            runtime_metadata.setdefault("executor_kind", executor_kind)
        record = {
            "protocol_version": IDENTITY_PROBE_PROTOCOL_VERSION,
            "experiment_root": str(experiment_root),
            "batch_id": manifest["batch_id"],
            "arm": manifest["arm"],
            "call_index": calls_made - 1,
            "arm_config_hash": arm_hash,
            "record_valid": record_valid,
            "per_call_status": per_call_status,
            "uncached_input_tokens": (
                usage["uncached_input_tokens"] if usage is not None else None
            ),
            "cached_input_tokens": (
                usage["cached_input_tokens"] if usage is not None else None
            ),
            "output_tokens": usage["output_tokens"] if usage is not None else None,
            "cache_status": cache_status,
            "runtime_metadata": runtime_metadata,
            "observed_runtime_identity": observed,
            "model_text_output": model_text,
            "calls_made": calls_made,
            "tokens_used": tokens_used,
        }
        if not record_valid:
            record["invalid_reason"] = "USAGE_FIELDS_MISSING"
            stop_reason = "USAGE_AUTHORITY_UNAVAILABLE"
        records.append(record)
        _append_jsonl(jsonl_path, record)
        if per_call_status == "PER_CALL_CAP_EXCEEDED" or not record_valid:
            break
    for record in records:
        record["stop_reason"] = stop_reason
        record["calls_made"] = calls_made
        record["tokens_used"] = tokens_used
    summary = aggregate_identity_probe_results(records)
    _write_json(arm_dir / "summary.json", summary)
    _write_json(
        arm_dir / "run.json",
        {
            "protocol_version": IDENTITY_PROBE_PROTOCOL_VERSION,
            "experiment_root": str(experiment_root),
            "calls_made": calls_made,
            "tokens_used": tokens_used,
            "stop_reason": stop_reason,
            "arm": manifest["arm"],
            "batch_id": manifest["batch_id"],
        },
    )
    return records


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
