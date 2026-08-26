"""Cost evidence normalization and deterministic paired-case aggregation.

The cost ledger is intentionally attempt-oriented.  Every failed call, retry,
review pass, and execution surface remains visible in the input sequence;
summary values are derived copies and never rewrite the original attempts.
Token values are accepted only when the runtime supplied a finite,
non-negative number.  Missing usage is represented as unavailable rather than
estimated.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Mapping
from typing import Any

try:
    from .ai_workflow_artifacts import (
        CostEvidence,
        EVIDENCE_CLASSES,
        EXECUTION_SURFACES,
        ROLES,
        ROUTES,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        CostEvidence,
        EVIDENCE_CLASSES,
        EXECUTION_SURFACES,
        ROLES,
        ROUTES,
    )


class CostError(RuntimeError):
    """Self-contained fallback error for direct module use.

    Calls made through the public ``ai_workflow`` facade raise its existing
    ``WorkflowError`` instead.  Keeping this small fallback avoids a module
    import cycle when this focused module is used as a script.
    """

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


_ALIASES = {
    "pair": "paired_case_id",
    "paired_case": "paired_case_id",
    "case_id": "paired_case_id",
    "surface": "execution_surface",
    "executionSurface": "execution_surface",
    "status": "quality_outcome",
    "quality": "quality_outcome",
    "duration": "duration_seconds",
    "prompt_size": "prompt_bytes",
    "input_token_count": "input_tokens",
    "cached_input_token_count": "cached_input_tokens",
    "output_token_count": "output_tokens",
    "retry": "retry_kind",
}
_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
_PRICE_FIELDS = frozenset(
    {
        "cost",
        "cost_usd",
        "amount_usd",
        "credits",
        "price",
        "projected_cost",
        "projected_cost_usd",
        "projected_price",
        "projected_price_usd",
        "estimated_cost",
        "estimated_cost_usd",
        "estimated_price",
        "estimated_price_usd",
        "input_cost",
        "input_cost_usd",
        "output_cost",
        "output_cost_usd",
        "measured_cost",
        "measured_cost_usd",
        "baseline_cost",
        "baseline_cost_usd",
        "new_cost",
        "new_cost_usd",
        "baseline_measured_cost",
        "new_measured_cost",
    }
)
# Every accepted price/cost value is projection evidence.  Keeping this as
# the full closed set prevents a newly named price field from silently
# entering measured evidence.
_PROJECTED_PRICE_FIELDS = _PRICE_FIELDS
_OPTIONAL_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "route",
        "role",
        "execution_surface",
        "duration_seconds",
        "prompt_bytes",
        "prompt",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "retry_kind",
        "verification_seconds",
        "quality_outcome",
        "paired_case_id",
        "evidence_class",
        "rate_snapshot_id",
        "attempt_id",
        "task_id",
        "timestamp_utc",
        "evidence_origin",
        "failed",
        "baseline_quality_points",
        "new_quality_points",
        "quality_delta_points",
        "net_measured_cost_delta",
        "_status",
    }
)
_SUMMARY_META_KEYS = frozenset(
    {"summary", "metadata", "paired_case_count", "quality_delta_points", "net_measured_cost_delta"}
)


def _workflow_exception(code: str, message: str) -> BaseException:
    """Construct ``WorkflowError`` lazily so imports remain acyclic."""

    try:
        from .ai_workflow import WorkflowError
    except (ImportError, ModuleNotFoundError):
        try:
            from ai_workflow import WorkflowError
        except (ImportError, ModuleNotFoundError):
            return CostError(code, message)
    return WorkflowError(code, message)


def _fail(code: str, message: str) -> None:
    raise _workflow_exception(code, message)


def finite_nonnegative_or_none(value: object, field: str) -> int | float | None:
    """Validate one runtime number without coercion or estimation."""

    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        _fail("COST_EVIDENCE_INVALID", f"{field} must be a finite non-negative number")
    return value


def finite_signed_or_none(value: object, field: str) -> int | float | None:
    """Validate one finite numeric delta while allowing either sign."""

    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        _fail("COST_EVIDENCE_INVALID", f"{field} must be a finite number")
    return value


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if dataclasses.is_dataclass(value):
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            result = to_dict()
            if isinstance(result, Mapping):
                return dict(result)
    _fail("COST_EVIDENCE_INVALID", "cost evidence must be an object")
    raise AssertionError("unreachable")


def _nonempty_string(value: object, field: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        _fail("COST_EVIDENCE_INVALID", f"{field} must be a non-empty string")
    return value


def _normal_form(value: object) -> dict[str, object]:
    raw = _mapping(value)
    normalized = dict(raw)
    for alias, canonical in _ALIASES.items():
        if alias in normalized:
            # Runtime attempt helpers often retain a canonical default while
            # exposing a concise alias (``surface``, ``pair``, ``status``).
            # The explicit alias is the caller's latest value.  Keep status
            # separately so failed attempts remain countable even when a
            # quality outcome was also supplied.
            if alias == "status":
                normalized["_status"] = normalized[alias]
            else:
                normalized[canonical] = normalized[alias]
            normalized.pop(alias, None)
    unknown = sorted(
        key
        for key in normalized
        if key not in _OPTIONAL_INPUT_FIELDS and key not in _PRICE_FIELDS
    )
    if unknown:
        _fail("COST_EVIDENCE_INVALID", f"unsupported cost field {unknown[0]}")
    return normalized


def _price_values(value: Mapping[str, object]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for field in sorted(_PRICE_FIELDS):
        if field not in value:
            continue
        number = finite_nonnegative_or_none(value[field], field)
        if number is not None:
            result[field] = number
    return result


def _is_explicit_measured(value: Mapping[str, object]) -> bool:
    return any(field in value and value[field] is not None for field in (*_TOKEN_FIELDS, "duration_seconds"))


def _normalize_evidence_class(
    value: Mapping[str, object], measured: bool, prices: Mapping[str, object]
) -> str:
    explicit = value.get("evidence_class")
    if explicit is None or explicit == "":
        evidence_class = "measured" if measured else "unavailable"
        if value.get("rate_snapshot_id") is not None:
            evidence_class = "sample_validated_projection"
    else:
        evidence_class = explicit
    if not isinstance(evidence_class, str) or evidence_class not in EVIDENCE_CLASSES:
        _fail("COST_EVIDENCE_INVALID", "evidence_class is not supported")
    if evidence_class == "measured" and not measured:
        _fail(
            "COST_EVIDENCE_INVALID",
            "measured evidence requires explicit usage or duration",
        )
    projected_prices = {
        field
        for field in value
        if field in _PROJECTED_PRICE_FIELDS and value[field] is not None
    }
    if projected_prices and evidence_class != "sample_validated_projection":
        _fail(
            "COST_EVIDENCE_INVALID",
            "projected price requires sample_validated_projection evidence",
        )
    if projected_prices and measured:
        _fail(
            "COST_EVIDENCE_INVALID",
            "measured usage cannot be mixed with projected price",
        )
    rate_snapshot_id = value.get("rate_snapshot_id")
    if evidence_class == "sample_validated_projection":
        _nonempty_string(rate_snapshot_id, "rate_snapshot_id")
    elif rate_snapshot_id is not None:
        _fail("COST_EVIDENCE_INVALID", "rate_snapshot_id requires projection evidence")
    if evidence_class == "unavailable" and prices:
        _fail(
            "COST_EVIDENCE_INVALID",
            "unavailable evidence cannot contain projected price values",
        )
    return evidence_class


def normalize_cost_evidence(value: object) -> CostEvidence:
    """Normalize one attempt into the versioned ``CostEvidence`` value object."""

    raw = _normal_form(value)
    schema_version = raw.get("schema_version", "cost-evidence-1")
    if schema_version != "cost-evidence-1":
        _fail("COST_EVIDENCE_INVALID", "schema_version must be cost-evidence-1")
    route = _nonempty_string(raw.get("route"), "route", default="delegated")
    if route not in ROUTES:
        _fail("COST_EVIDENCE_INVALID", "route is not supported")
    role = _nonempty_string(raw.get("role"), "role")
    if role not in (ROLES - {"host"}):
        _fail("COST_EVIDENCE_INVALID", "role is not supported")
    surface = _nonempty_string(
        raw.get("execution_surface"),
        "execution_surface",
        default="CODEX_EXEC_ROLE_CONTRACT",
    )
    if surface not in EXECUTION_SURFACES:
        _fail("COST_EVIDENCE_INVALID", "execution_surface is not supported")

    duration_raw = raw.get("duration_seconds")
    duration = finite_nonnegative_or_none(duration_raw, "duration_seconds")
    prompt_raw = raw.get("prompt_bytes")
    if prompt_raw is None and "prompt" in raw:
        prompt = raw["prompt"]
        if not isinstance(prompt, str):
            _fail("COST_EVIDENCE_INVALID", "prompt must be a string")
        prompt_raw = len(prompt.encode("utf-8"))
    prompt_number = finite_nonnegative_or_none(prompt_raw, "prompt_bytes")
    if prompt_number is None:
        prompt_bytes = 0
    elif not isinstance(prompt_number, int):
        _fail("COST_EVIDENCE_INVALID", "prompt_bytes must be an integer")
    else:
        prompt_bytes = prompt_number

    tokens: dict[str, int | float | None] = {}
    for field in _TOKEN_FIELDS:
        tokens[field] = finite_nonnegative_or_none(raw.get(field), field)
    verification = finite_nonnegative_or_none(
        raw.get("verification_seconds"), "verification_seconds"
    )
    retry_kind = _nonempty_string(raw.get("retry_kind"), "retry_kind", default="none")
    quality_outcome = _nonempty_string(
        raw.get("quality_outcome"), "quality_outcome", default="UNKNOWN"
    )
    paired_case_id = raw.get("paired_case_id")
    if paired_case_id is not None:
        _nonempty_string(paired_case_id, "paired_case_id")
    rate_snapshot_id = raw.get("rate_snapshot_id")
    if rate_snapshot_id is not None:
        _nonempty_string(rate_snapshot_id, "rate_snapshot_id")
    measured = _is_explicit_measured(raw)
    prices = _price_values(raw)
    evidence_class = _normalize_evidence_class(raw, measured, prices)
    if evidence_class != "sample_validated_projection":
        rate_snapshot_id = None

    evidence = CostEvidence(
        schema_version="cost-evidence-1",
        route=route,
        role=role,
        execution_surface=surface,
        duration_seconds=0 if duration is None else duration,
        prompt_bytes=prompt_bytes,
        input_tokens=tokens["input_tokens"],
        cached_input_tokens=tokens["cached_input_tokens"],
        output_tokens=tokens["output_tokens"],
        retry_kind=retry_kind,
        verification_seconds=0 if verification is None else verification,
        quality_outcome=quality_outcome,
        paired_case_id=paired_case_id,
        evidence_class=evidence_class,
        rate_snapshot_id=rate_snapshot_id,
    )
    return evidence


def _raw_cost_fields(value: object) -> dict[str, object]:
    """Return a non-mutating copy used for optional aggregate metadata."""

    return _normal_form(value)


def _number_for_sum(value: object, field: str) -> int | float | None:
    return finite_nonnegative_or_none(value, field)


def _case_template(pair_id: str) -> dict[str, object]:
    return {
        "paired_case_id": pair_id,
        "attempt_count": 0,
        "failed_attempts": 0,
        "technical_retries": 0,
        "retry_overhead": 0,
        "measured_attempt_count": 0,
        "projection_attempt_count": 0,
        "unavailable_attempt_count": 0,
        "measured_input_tokens": 0,
        "measured_cached_input_tokens": 0,
        "measured_output_tokens": 0,
        "measured_duration_seconds": 0,
        "measured_verification_seconds": 0,
        "measured_prompt_bytes": 0,
        "duration_seconds": 0,
        "verification_seconds": 0,
        "prompt_bytes": 0,
        "projected_cost": None,
        "rate_snapshot_ids": [],
        "net_measured_cost_delta": None,
        "quality_delta_points": None,
        "routes": [],
        "roles": [],
        "surfaces": [],
        "evidence_classes": [],
        "quality_outcomes": [],
        "attempts": [],
        "raw_attempts": [],
    }


def _append_unique(target: list[object], value: object) -> None:
    if value not in target:
        target.append(value)


def _cost_value(raw: Mapping[str, object], *fields: str) -> int | float | None:
    for field in fields:
        if field in raw:
            if field in {"net_measured_cost_delta", "quality_delta_points"}:
                return finite_signed_or_none(raw[field], field)
            return _number_for_sum(raw[field], field)
    return None


def aggregate_paired_cases(records: Iterable[object]) -> dict[str, dict[str, object]]:
    """Aggregate all attempts by their pre-registered paired-case identity."""

    if isinstance(records, (str, bytes, Mapping)):
        _fail("COST_EVIDENCE_INVALID", "records must be an iterable of attempts")
    try:
        iterator = iter(records)
    except TypeError:
        _fail("COST_EVIDENCE_INVALID", "records must be an iterable of attempts")
    cases: dict[str, dict[str, object]] = {}
    for record in iterator:
        original = _mapping(record)
        raw = _raw_cost_fields(record)
        evidence = normalize_cost_evidence(raw)
        pair_id = evidence.paired_case_id
        if pair_id is None:
            _fail("COST_EVIDENCE_INVALID", "paired_case_id is required for aggregation")
        case = cases.setdefault(pair_id, _case_template(pair_id))
        case["attempt_count"] += 1
        status = raw.get("_status", raw.get("quality_outcome"))
        if status == "FAILED" or raw.get("failed") is True:
            case["failed_attempts"] += 1
        if evidence.retry_kind != "none":
            case["retry_overhead"] += 1
        if evidence.retry_kind == "technical":
            case["technical_retries"] += 1
        case["duration_seconds"] += evidence.duration_seconds
        case["verification_seconds"] += evidence.verification_seconds
        case["prompt_bytes"] += evidence.prompt_bytes
        if evidence.evidence_class == "measured":
            case["measured_attempt_count"] += 1
            for field, summary_field in (
                ("input_tokens", "measured_input_tokens"),
                ("cached_input_tokens", "measured_cached_input_tokens"),
                ("output_tokens", "measured_output_tokens"),
            ):
                number = getattr(evidence, field)
                if number is not None:
                    case[summary_field] += number
            case["measured_duration_seconds"] += evidence.duration_seconds
            case["measured_verification_seconds"] += evidence.verification_seconds
            case["measured_prompt_bytes"] += evidence.prompt_bytes
        elif evidence.evidence_class == "sample_validated_projection":
            case["projection_attempt_count"] += 1
        else:
            case["unavailable_attempt_count"] += 1
        _append_unique(case["routes"], evidence.route)
        _append_unique(case["roles"], evidence.role)
        _append_unique(case["surfaces"], evidence.execution_surface)
        _append_unique(case["evidence_classes"], evidence.evidence_class)
        _append_unique(case["quality_outcomes"], evidence.quality_outcome)
        case["raw_attempts"].append(dict(original))

        projected_cost = _cost_value(raw, "projected_cost_usd", "projected_cost", "estimated_cost_usd")
        if projected_cost is not None:
            case["projected_cost"] = (case["projected_cost"] or 0) + projected_cost
        if evidence.rate_snapshot_id is not None:
            _append_unique(case["rate_snapshot_ids"], evidence.rate_snapshot_id)
        measured_cost = _cost_value(raw, "net_measured_cost_delta")
        if measured_cost is None:
            baseline = _cost_value(raw, "baseline_measured_cost", "baseline_cost_usd", "baseline_cost")
            current = _cost_value(raw, "new_measured_cost", "new_cost_usd", "new_cost")
            if baseline is not None and current is not None:
                measured_cost = current - baseline
        if measured_cost is not None:
            case["net_measured_cost_delta"] = (
                (case["net_measured_cost_delta"] or 0) + measured_cost
            )
        quality_delta = _cost_value(raw, "quality_delta_points")
        if quality_delta is None:
            baseline_quality = _cost_value(raw, "baseline_quality_points")
            current_quality = _cost_value(raw, "new_quality_points")
            if baseline_quality is not None and current_quality is not None:
                quality_delta = current_quality - baseline_quality
        if quality_delta is not None:
            case["quality_delta_points"] = (case["quality_delta_points"] or 0) + quality_delta
        case["attempts"].append(evidence.to_dict())

    result: dict[str, dict[str, object]] = {}
    for pair_id in sorted(cases):
        case = cases[pair_id]
        case["routes"] = sorted(case["routes"])
        case["roles"] = sorted(case["roles"])
        case["surfaces"] = sorted(case["surfaces"])
        case["evidence_classes"] = sorted(case["evidence_classes"])
        case["quality_outcomes"] = sorted(case["quality_outcomes"])
        case["route"] = case["routes"][0] if len(case["routes"]) == 1 else "mixed"
        case["execution_surface"] = (
            case["surfaces"][0] if len(case["surfaces"]) == 1 else "mixed"
        )
        result[pair_id] = case
    return result


def _summary_value(summary: Mapping[str, object], field: str, default: object) -> object:
    if field in summary:
        return summary[field]
    nested = summary.get("summary")
    if isinstance(nested, Mapping) and field in nested:
        return nested[field]
    return default


def evaluate_cost_claim(
    summary: Mapping[str, object],
    minimum_cases: int = 30,
    quality_margin_points: float = 5.0,
) -> str:
    """Apply the closed-set paired-case gate without estimating missing values."""

    if not isinstance(summary, Mapping):
        _fail("COST_EVIDENCE_INVALID", "summary must be an object")
    if isinstance(minimum_cases, bool) or not isinstance(minimum_cases, int) or minimum_cases < 0:
        _fail("COST_EVIDENCE_INVALID", "minimum_cases must be a non-negative integer")
    margin = finite_nonnegative_or_none(quality_margin_points, "quality_margin_points")
    assert margin is not None
    pair_keys = [
        key
        for key, value in summary.items()
        if isinstance(key, str)
        and key not in _SUMMARY_META_KEYS
        and isinstance(value, Mapping)
    ]
    paired_case_count = _summary_value(summary, "paired_case_count", len(pair_keys))
    if isinstance(paired_case_count, bool) or not isinstance(paired_case_count, int) or paired_case_count < 0:
        _fail("COST_EVIDENCE_INVALID", "paired_case_count must be a non-negative integer")
    quality = _summary_value(summary, "quality_delta_points", None)
    net = _summary_value(summary, "net_measured_cost_delta", None)
    if quality is None and pair_keys:
        values = [
            value.get("quality_delta_points")
            for key in pair_keys
            if isinstance(summary[key], Mapping)
            for value in [summary[key]]
            if value.get("quality_delta_points") is not None
        ]
        quality = sum(values) if values else 0.0
    if net is None and pair_keys:
        values = [
            value.get("net_measured_cost_delta")
            for key in pair_keys
            if isinstance(summary[key], Mapping)
            for value in [summary[key]]
            if value.get("net_measured_cost_delta") is not None
        ]
        # A delta is only proven when every paired case supplies one.
        if len(values) == len(pair_keys):
            net = sum(values)
    if quality is None:
        quality_value = 0.0
    else:
        if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not math.isfinite(quality):
            _fail("COST_EVIDENCE_INVALID", "quality_delta_points must be finite")
        quality_value = quality
    if net is not None:
        if isinstance(net, bool) or not isinstance(net, (int, float)) or not math.isfinite(net):
            _fail("COST_EVIDENCE_INVALID", "net_measured_cost_delta must be finite")
    if paired_case_count < minimum_cases:
        return "OBSERVATION_ONLY"
    if quality_value < -margin:
        return "QUALITY_REGRESSION"
    if net is None or net >= 0:
        return "NO_COST_REDUCTION_PROVEN"
    return "COST_REDUCTION_SUPPORTED"


def evaluate_optimization_gate(
    metrics: Mapping[str, object],
    minimum_cases: int = 8,
    quality_margin_points: float = 5.0,
) -> str:
    """Allow enforced advice only when every measured gate is fully true."""

    if not isinstance(metrics, Mapping) or metrics.get("synthetic") is True:
        return "FALLBACK_FIXED"
    cost_summary = metrics.get("cost_summary")
    if not isinstance(cost_summary, Mapping):
        return "FALLBACK_FIXED"
    try:
        claim = evaluate_cost_claim(
            cost_summary,
            minimum_cases=minimum_cases,
            quality_margin_points=quality_margin_points,
        )
    except Exception:
        return "FALLBACK_FIXED"
    if claim != "COST_REDUCTION_SUPPORTED":
        return "FALLBACK_FIXED"
    for field in ("p0_miss_count", "p1_miss_count"):
        value = metrics.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return "FALLBACK_FIXED"
    calibration = metrics.get("calibration_first_delivery_pass_rate")
    experiment = metrics.get("experiment_first_delivery_pass_rate")
    if (
        isinstance(calibration, bool)
        or not isinstance(calibration, (int, float))
        or not math.isfinite(calibration)
        or isinstance(experiment, bool)
        or not isinstance(experiment, (int, float))
        or not math.isfinite(experiment)
        or experiment < calibration
    ):
        return "FALLBACK_FIXED"
    return "ALLOW_ENFORCED"


def _case_category(case: Mapping[str, object]) -> tuple[str, ...]:
    categories: list[str] = []
    if case.get("measured_attempt_count", 0):
        categories.append("measured")
    if case.get("projection_attempt_count", 0):
        categories.append("projection")
    if case.get("unavailable_attempt_count", 0) or not categories:
        categories.append("unavailable")
    return tuple(categories)


def _claim_summary_from_cases(summary: Mapping[str, object]) -> dict[str, object]:
    pairs = [
        value
        for key, value in summary.items()
        if key not in _SUMMARY_META_KEYS and isinstance(value, Mapping)
    ]
    quality_values = [
        value.get("quality_delta_points")
        for value in pairs
        if isinstance(value.get("quality_delta_points"), (int, float))
    ]
    delta_values = [
        value.get("net_measured_cost_delta")
        for value in pairs
        if isinstance(value.get("net_measured_cost_delta"), (int, float))
    ]
    return {
        "paired_case_count": len(pairs),
        "quality_delta_points": sum(quality_values) if quality_values else 0.0,
        "net_measured_cost_delta": (
            sum(delta_values) if len(delta_values) == len(pairs) and pairs else None
        ),
    }


def render_cost_sections(
    summary: Mapping[str, object] | Iterable[object] | None,
    claim_summary: Mapping[str, object] | None = None,
    unavailable_attempts: int = 0,
) -> str:
    """Render measured, projection, and unavailable evidence as separate sections."""

    if summary is None:
        normalized: dict[str, dict[str, object]] = {}
    elif isinstance(summary, Mapping):
        normalized = {
            str(key): dict(value)
            for key, value in summary.items()
            if isinstance(key, str)
            and key not in _SUMMARY_META_KEYS
            and isinstance(value, Mapping)
        }
    else:
        normalized = aggregate_paired_cases(summary)
    lines = [
        "## Cost Evidence",
        "",
        f"- paired-case count: {len(normalized)}",
    ]
    claim_input = dict(claim_summary) if isinstance(claim_summary, Mapping) else _claim_summary_from_cases(normalized)
    lines.append(f"- claim gate: {evaluate_cost_claim(claim_input)}")
    for heading, category in (("Measured", "measured"), ("Projection", "projection"), ("Unavailable", "unavailable")):
        lines.extend(("", f"## {heading}", ""))
        entries = [
            (pair_id, case)
            for pair_id, case in sorted(normalized.items())
            if category in _case_category(case)
        ]
        if not entries:
            lines.append("- None")
            if category == "unavailable" and unavailable_attempts:
                lines.append(f"- unavailable attempts: {unavailable_attempts}")
            continue
        for pair_id, case in entries:
            detail = (
                "- "
                + pair_id
                + ": route="
                + str(case.get("route", ",".join(case.get("routes", []))))
                + "; execution surface="
                + str(case.get("execution_surface", ",".join(case.get("surfaces", []))))
                + "; retry overhead="
                + str(case.get("retry_overhead", 0))
                + "; prompt bytes="
                + str(case.get("prompt_bytes", case.get("measured_prompt_bytes", 0)))
                + "; paired-case count=1"
            )
            if category == "measured":
                detail += "; measured input tokens: " + str(
                    case.get("measured_input_tokens", 0)
                )
                detail += "; measured cached input tokens: " + str(
                    case.get("measured_cached_input_tokens", 0)
                )
                detail += "; measured output tokens: " + str(
                    case.get("measured_output_tokens", 0)
                )
                detail += "; measured duration seconds: " + str(
                    case.get("measured_duration_seconds", 0)
                )
                detail += "; net measured cost delta: " + str(
                    case.get("net_measured_cost_delta")
                )
                detail += "; quality delta points: " + str(case.get("quality_delta_points"))
            if category == "projection":
                detail += "; projected cost: " + str(case.get("projected_cost"))
                rates = case.get("rate_snapshot_ids", [])
                detail += "; rate snapshot: " + str(",".join(rates) if rates else None)
            if category == "unavailable":
                detail += "; unavailable attempts: " + str(
                    case.get("unavailable_attempt_count", 0)
                )
            lines.append(detail)
        if category == "unavailable" and unavailable_attempts:
            lines.append(f"- unavailable attempts: {unavailable_attempts}")
    return "\n".join(lines) + "\n"


__all__ = [
    "CostError",
    "CostEvidence",
    "aggregate_paired_cases",
    "evaluate_cost_claim",
    "evaluate_optimization_gate",
    "finite_nonnegative_or_none",
    "finite_signed_or_none",
    "normalize_cost_evidence",
    "render_cost_sections",
]
