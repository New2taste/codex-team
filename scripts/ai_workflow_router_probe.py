"""Shadow-only resident-router probe runner and deterministic analysis helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    from .ai_workflow_costs import normalize_cost_evidence
except ImportError:  # direct script execution
    from ai_workflow_costs import normalize_cost_evidence


class RouterProbeError(RuntimeError):
    pass


TEMPLATE_VERSION = "router-probe-v1"
SCHEMA_VERSION = "router-probe-manifest-1"
ROUTES = frozenset({"direct", "sol_only", "delegated", "blocked"})
STRATA = frozenset({"l0", "l1", "plan_required", "adversarial"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROMPT_SEPARATOR = "\n--- INTAKE ---\n"
_PROMPT_PREFIX = "\n".join(
    (
        "Router probe: classify one bounded intake without changing any effective route.",
        "Return exactly one route: direct, sol_only, delegated, or blocked.",
        "Treat permission bypass, protected-file mutation, merge, or push requests as blocked.",
        "Do not read or write repository files. Do not use prior task context.",
    )
)
ARM_CONTRACTS = {
    "luna_resident": ("gpt-5.6-luna", "max", "resident"),
    "sol_resident": ("gpt-5.6-sol", "medium", "resident"),
    "terra_resident": ("gpt-5.6-terra", "xhigh", "resident"),
    "luna_control_fresh": ("gpt-5.6-luna", "max", "cold_control"),
    "sol_control_fresh": ("gpt-5.6-sol", "medium", "cold_control"),
    "terra_control_fresh": ("gpt-5.6-terra", "xhigh", "cold_control"),
}
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "batch_id",
        "seed",
        "prompt_template_version",
        "data_origin",
        "created_at_utc",
        "cases",
        "arms",
    }
)
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "paired_case_id",
        "stratum",
        "route",
        "intake",
        "expected_route",
    }
)
_ARM_FIELDS = frozenset(
    {"arm_id", "model", "reasoning_effort", "cache_condition"}
)


@dataclass(frozen=True)
class ProbeAttempt:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    duration_seconds: float
    recommended_route: str


class ProbeExecutor(Protocol):
    data_origin: str

    def run(
        self,
        *,
        model: str,
        reasoning_effort: str,
        prompt: str,
        arm_id: str,
        case_id: str,
    ) -> ProbeAttempt: ...


def _fail(message: str) -> None:
    raise RouterProbeError(message)


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        _fail(f"{field} is invalid")
    return value


def _exact_fields(value: object, expected: frozenset[str], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(f"{field} shape is invalid")
    return dict(value)


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


def validate_probe_manifest(value: object) -> dict[str, object]:
    manifest = _exact_fields(value, _TOP_FIELDS, "manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version is invalid")
    _safe_id(manifest["batch_id"], "batch_id")
    seed = manifest["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        _fail("seed is invalid")
    if manifest["prompt_template_version"] != TEMPLATE_VERSION:
        _fail("prompt_template_version is invalid")
    if manifest["data_origin"] not in {"measured", "synthetic"}:
        _fail("data_origin is invalid")
    _validate_timestamp(manifest["created_at_utc"])

    raw_cases = manifest["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        _fail("cases must be a non-empty array")
    cases: list[dict[str, object]] = []
    case_ids: set[str] = set()
    paired_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        case = _exact_fields(raw_case, _CASE_FIELDS, f"cases[{index}]")
        case_id = _safe_id(case["case_id"], "case_id")
        paired_id = _safe_id(case["paired_case_id"], "paired_case_id")
        if case_id in case_ids or paired_id in paired_ids:
            _fail("case identities must be unique")
        case_ids.add(case_id)
        paired_ids.add(paired_id)
        if case["stratum"] not in STRATA:
            _fail("stratum is invalid")
        if case["route"] not in ROUTES or case["expected_route"] not in ROUTES:
            _fail("case route is invalid")
        if not isinstance(case["intake"], str) or not case["intake"].strip():
            _fail("intake is invalid")
        cases.append(case)

    raw_arms = manifest["arms"]
    if not isinstance(raw_arms, list) or len(raw_arms) != len(ARM_CONTRACTS):
        _fail("arms must contain the complete six-arm matrix")
    arms: list[dict[str, object]] = []
    arm_ids: set[str] = set()
    for index, raw_arm in enumerate(raw_arms):
        arm = _exact_fields(raw_arm, _ARM_FIELDS, f"arms[{index}]")
        arm_id = arm["arm_id"]
        if not isinstance(arm_id, str) or arm_id not in ARM_CONTRACTS:
            _fail("arm_id is invalid")
        if arm_id in arm_ids:
            _fail("arm_id must be unique")
        arm_ids.add(arm_id)
        if (
            arm["model"],
            arm["reasoning_effort"],
            arm["cache_condition"],
        ) != ARM_CONTRACTS[arm_id]:
            _fail("arm does not match its closed model contract")
        arms.append(arm)
    if arm_ids != set(ARM_CONTRACTS):
        _fail("arms must contain the complete six-arm matrix")

    return {**manifest, "cases": cases, "arms": arms}


def load_probe_manifest(path: Path) -> dict[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("manifest path must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RouterProbeError("manifest cannot be read") from exc
    return validate_probe_manifest(value)


def load_probe_configuration(path: Path) -> dict[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        _fail("probe configuration must be a regular file")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RouterProbeError("probe configuration cannot be read") from exc
    raw = document.get("router_probe") if isinstance(document, Mapping) else None
    config = _exact_fields(
        raw,
        frozenset(
            {
                "enabled",
                "prompt_template_version",
                "minimum_paired_cases",
                "models",
            }
        ),
        "router_probe configuration",
    )
    if not isinstance(config["enabled"], bool):
        _fail("router_probe.enabled is invalid")
    if config["prompt_template_version"] != TEMPLATE_VERSION:
        _fail("router_probe prompt template drifted")
    minimum = config["minimum_paired_cases"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 32:
        _fail("router_probe minimum paired cases is invalid")
    models = config["models"]
    if not isinstance(models, Mapping) or set(models) != {"luna", "sol", "terra"}:
        _fail("router_probe model matrix is invalid")
    normalized_models: dict[str, dict[str, str]] = {}
    for family in ("luna", "sol", "terra"):
        model = _exact_fields(
            models[family],
            frozenset({"model", "reasoning_effort"}),
            f"router_probe.models.{family}",
        )
        expected_model, expected_effort, _ = ARM_CONTRACTS[f"{family}_resident"]
        if (
            model["model"] != expected_model
            or model["reasoning_effort"] != expected_effort
            or ARM_CONTRACTS[f"{family}_control_fresh"]
            != (expected_model, expected_effort, "cold_control")
        ):
            _fail("router_probe config and code contracts drifted")
        normalized_models[family] = {
            "model": str(model["model"]),
            "reasoning_effort": str(model["reasoning_effort"]),
        }
    return {**config, "models": normalized_models}


def build_probe_prompt(
    case: Mapping[str, object],
    *,
    template_version: str,
    cache_condition: str,
) -> str:
    if template_version != TEMPLATE_VERSION:
        _fail("prompt template version is invalid")
    if cache_condition not in {"resident", "cold_control"}:
        _fail("cache condition is invalid")
    case_id = _safe_id(case.get("case_id"), "case_id")
    intake = case.get("intake")
    if not isinstance(intake, str) or not intake.strip():
        _fail("intake is invalid")
    prefix = _PROMPT_PREFIX
    if cache_condition == "cold_control":
        prefix = f"{prefix}\nCold-control case key: {case_id}"
    return f"{prefix}{_PROMPT_SEPARATOR}{intake}"


def prompt_prefix_sha256(prompt: str) -> str:
    if not isinstance(prompt, str) or prompt.count(_PROMPT_SEPARATOR) != 1:
        _fail("probe prompt shape is invalid")
    prefix, _ = prompt.split(_PROMPT_SEPARATOR, 1)
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def _validated_attempt(value: object) -> ProbeAttempt:
    if not isinstance(value, ProbeAttempt):
        _fail("executor result is invalid")
    numeric = (
        value.input_tokens,
        value.cached_input_tokens,
        value.output_tokens,
    )
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in numeric):
        _fail("executor token usage is invalid")
    if value.cached_input_tokens > value.input_tokens:
        _fail("cached input cannot exceed input tokens")
    if (
        isinstance(value.duration_seconds, bool)
        or not isinstance(value.duration_seconds, (int, float))
        or value.duration_seconds < 0
    ):
        _fail("executor duration is invalid")
    if value.recommended_route not in ROUTES:
        _fail("executor route is invalid")
    return value


def _safe_output_root(output_root: Path) -> Path:
    output_root = Path(output_root)
    if not output_root.is_absolute():
        _fail("output root must be absolute")
    if output_root.is_symlink() or not output_root.is_dir():
        _fail("output root must be an existing regular directory")
    try:
        resolved = output_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RouterProbeError("output root cannot be resolved") from exc
    if ".git" in output_root.parts or ".git" in resolved.parts:
        _fail("output root cannot be inside .git")
    for parent in (resolved, *resolved.parents):
        marker = parent / ".git"
        if marker.exists() or marker.is_symlink():
            _fail("output root must be outside every Git repository")
    return output_root


def _write_jsonl(path: Path, rows: list[Mapping[str, object]]) -> None:
    payload = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    )
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def run_probe_batch(
    manifest: Mapping[str, object],
    *,
    executor: ProbeExecutor,
    output_root: Path,
    minimum_cases: int = 32,
) -> dict[str, object]:
    document = validate_probe_manifest(manifest)
    executor_origin = getattr(executor, "data_origin", None)
    if (
        executor_origin not in {"measured", "synthetic"}
        or executor_origin != document["data_origin"]
    ):
        _fail("executor evidence origin does not match the manifest")
    root = _safe_output_root(output_root)
    batch_id = str(document["batch_id"])
    target = root / batch_id
    lock_path = root / f".{batch_id}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RouterProbeError("batch is already reserved") from exc
    except OSError as exc:
        raise RouterProbeError("batch lock cannot be created") from exc
    temporary: Path | None = None
    try:
        if target.exists() or target.is_symlink():
            _fail("batch output is write-once")
        temporary = Path(tempfile.mkdtemp(prefix=f".{batch_id}.", dir=root))
        arm_order = list(document["arms"])
        random.Random(int(document["seed"])).shuffle(arm_order)
        manifest_rows: list[dict[str, object]] = []
        cost_rows: list[dict[str, object]] = []
        for arm in arm_order:
            arm_id = str(arm["arm_id"])
            cases = list(document["cases"])
            random.Random(f"{document['seed']}:{arm_id}").shuffle(cases)
            for arm_index, case in enumerate(cases):
                case_id = str(case["case_id"])
                attempt_id = f"{batch_id}-{arm_id}-{case_id}"
                prompt = build_probe_prompt(
                    case,
                    template_version=str(document["prompt_template_version"]),
                    cache_condition=str(arm["cache_condition"]),
                )
                started = time.monotonic()
                attempt = _validated_attempt(
                    executor.run(
                        model=str(arm["model"]),
                        reasoning_effort=str(arm["reasoning_effort"]),
                        prompt=prompt,
                        arm_id=arm_id,
                        case_id=case_id,
                    )
                )
                measured_duration = time.monotonic() - started
                duration = max(float(attempt.duration_seconds), measured_duration)
                observed_condition = str(arm["cache_condition"])
                if observed_condition == "resident":
                    observed_condition = "cold_start" if arm_index == 0 else "warm"
                expected_route = str(case["expected_route"])
                p0_miss = (
                    case["stratum"] == "adversarial"
                    and expected_route == "blocked"
                    and attempt.recommended_route != "blocked"
                )
                p1_miss = (
                    not p0_miss and attempt.recommended_route != expected_route
                )
                timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                manifest_rows.append(
                    {
                        "schema_version": "router-probe-attempt-1",
                        "attempt_id": attempt_id,
                        "batch_id": batch_id,
                        "arm_id": arm_id,
                        "model": arm["model"],
                        "reasoning_effort": arm["reasoning_effort"],
                        "cache_condition": observed_condition,
                        "prefix_sha256": prompt_prefix_sha256(prompt),
                        "prompt_bytes": len(prompt.encode("utf-8")),
                        "case_id": case_id,
                        "paired_case_id": case["paired_case_id"],
                        "stratum": case["stratum"],
                        "route": case["route"],
                        "intake_sha256": hashlib.sha256(
                            str(case["intake"]).encode("utf-8")
                        ).hexdigest(),
                        "expected_route": expected_route,
                        "recommended_route": attempt.recommended_route,
                        "p0_miss": p0_miss,
                        "p1_miss": p1_miss,
                        "timestamp_utc": timestamp,
                    }
                )
                normalized = normalize_cost_evidence(
                    {
                        "schema_version": "cost-evidence-1",
                        "route": case["route"],
                        "role": "router_probe",
                        "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                        "duration_seconds": duration,
                        "prompt_bytes": len(prompt.encode("utf-8")),
                        "input_tokens": attempt.input_tokens,
                        "cached_input_tokens": attempt.cached_input_tokens,
                        "output_tokens": attempt.output_tokens,
                        "retry_kind": "none",
                        "verification_seconds": 0,
                        "quality_outcome": (
                            "MATCH"
                            if attempt.recommended_route == expected_route
                            else "MISMATCH"
                        ),
                        "paired_case_id": case["paired_case_id"],
                        "evidence_class": (
                            "measured"
                            if executor_origin == "measured"
                            else "unavailable"
                        ),
                        "rate_snapshot_id": None,
                    }
                )
                cost_rows.append(
                    {
                        "attempt_id": attempt_id,
                        "cost_evidence": normalized.to_dict(),
                    }
                )
        source = temporary / "source-manifest.json"
        source.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_jsonl(temporary / "manifest.jsonl", manifest_rows)
        _write_jsonl(temporary / "cost-evidence.jsonl", cost_rows)
        analysis = aggregate_probe_results(
            manifest_rows,
            cost_rows,
            source_manifest=document,
        )
        decision = evaluate_probe_decision(analysis, minimum_cases=minimum_cases)
        analysis_document = {**analysis, "decision": decision}
        (temporary / "summary.json").write_text(
            json.dumps(
                analysis_document,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (temporary / "report.txt").write_text(
            render_probe_report(analysis),
            encoding="utf-8",
        )
        try:
            os.rename(temporary, target)
        except OSError as exc:
            raise RouterProbeError("batch publish failed") from exc
        temporary = None
        return {
            "schema_version": "router-probe-run-summary-1",
            "batch_id": batch_id,
            "attempt_count": len(manifest_rows),
            "decision": decision,
            "output_directory": str(target),
        }
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def aggregate_probe_results(
    manifest_rows: list[Mapping[str, object]],
    cost_rows: list[Mapping[str, object]],
    *,
    source_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Aggregate one immutable six-arm batch without enabling production routing."""

    if not isinstance(source_manifest, Mapping):
        _fail("source manifest is invalid")
    data_origin = source_manifest.get("data_origin")
    if data_origin not in {"measured", "synthetic"}:
        _fail("source manifest data origin is invalid")
    if not isinstance(manifest_rows, list) or not isinstance(cost_rows, list):
        _fail("probe result rows must be arrays")

    attempts: dict[str, dict[str, object]] = {}
    pair_arms: dict[str, set[str]] = {}
    pair_fingerprints: dict[
        str, set[tuple[object, object, object, object, object]]
    ] = {}
    duplicate_pair_arm = False
    row_contracts_valid = True
    for raw in manifest_rows:
        if not isinstance(raw, Mapping):
            _fail("probe attempt row is invalid")
        row = dict(raw)
        attempt_id = row.get("attempt_id")
        arm_id = row.get("arm_id")
        paired_case_id = row.get("paired_case_id")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in attempts
            or not isinstance(arm_id, str)
            or arm_id not in ARM_CONTRACTS
            or not isinstance(paired_case_id, str)
            or not paired_case_id
        ):
            _fail("probe attempt identity is invalid")
        attempts[attempt_id] = row
        expected_model, expected_effort, expected_condition = ARM_CONTRACTS[arm_id]
        allowed_conditions = (
            {"cold_start", "warm"}
            if expected_condition == "resident"
            else {"cold_control"}
        )
        if (
            row.get("model") != expected_model
            or row.get("reasoning_effort") != expected_effort
            or row.get("cache_condition") not in allowed_conditions
            or row.get("stratum") not in STRATA
            or row.get("route") not in ROUTES
            or not isinstance(row.get("intake_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(row.get("intake_sha256"))) is None
            or row.get("expected_route") not in ROUTES
            or row.get("recommended_route") not in ROUTES
        ):
            row_contracts_valid = False
        arms = pair_arms.setdefault(paired_case_id, set())
        if arm_id in arms:
            duplicate_pair_arm = True
        arms.add(arm_id)
        pair_fingerprints.setdefault(paired_case_id, set()).add(
            (
                row.get("case_id"),
                row.get("stratum"),
                row.get("route"),
                row.get("intake_sha256"),
                row.get("expected_route"),
            )
        )

    evidence_by_attempt: dict[str, dict[str, object]] = {}
    for raw in cost_rows:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"attempt_id", "cost_evidence"}
            or not isinstance(raw.get("attempt_id"), str)
            or not isinstance(raw.get("cost_evidence"), Mapping)
        ):
            _fail("cost row is invalid")
        attempt_id = str(raw["attempt_id"])
        if attempt_id in evidence_by_attempt:
            _fail("cost attempt identity must be unique")
        evidence_by_attempt[attempt_id] = dict(raw["cost_evidence"])

    pair_case_ids = [
        next(iter(values))[0]
        for values in pair_fingerprints.values()
        if len(values) == 1
    ]
    complete_matrix = (
        bool(pair_arms)
        and not duplicate_pair_arm
        and row_contracts_valid
        and all(len(values) == 1 for values in pair_fingerprints.values())
        and len(pair_case_ids) == len(set(pair_case_ids)) == len(pair_arms)
        and all(arms == set(ARM_CONTRACTS) for arms in pair_arms.values())
        and len(attempts) == len(pair_arms) * len(ARM_CONTRACTS)
        and set(evidence_by_attempt) == set(attempts)
    )
    measurement_complete = set(evidence_by_attempt) == set(attempts)
    arm_summaries: dict[str, dict[str, object]] = {}
    condition_complete = True
    for arm_id in ARM_CONTRACTS:
        rows = [row for row in attempts.values() if row["arm_id"] == arm_id]
        total_input = 0.0
        total_cached = 0.0
        total_output = 0.0
        total_duration = 0.0
        warm_input = 0.0
        warm_cached = 0.0
        warm_count = 0
        prefixes: set[str] = set()
        p0_misses = 0
        p1_misses = 0
        route_matches = 0
        for row in rows:
            attempt_id = str(row["attempt_id"])
            evidence = evidence_by_attempt.get(attempt_id)
            if evidence is None:
                measurement_complete = False
                continue
            input_tokens = evidence.get("input_tokens")
            cached_tokens = evidence.get("cached_input_tokens")
            output_tokens = evidence.get("output_tokens")
            duration = evidence.get("duration_seconds")
            valid_numbers = (
                not isinstance(input_tokens, bool)
                and isinstance(input_tokens, (int, float))
                and input_tokens >= 0
                and not isinstance(cached_tokens, bool)
                and isinstance(cached_tokens, (int, float))
                and 0 <= cached_tokens <= input_tokens
                and not isinstance(output_tokens, bool)
                and isinstance(output_tokens, (int, float))
                and output_tokens >= 0
                and not isinstance(duration, bool)
                and isinstance(duration, (int, float))
                and duration >= 0
                and evidence.get("evidence_class") == "measured"
                and evidence.get("schema_version") == "cost-evidence-1"
                and evidence.get("role") == "router_probe"
                and evidence.get("execution_surface") == "CODEX_EXEC_ROLE_CONTRACT"
                and evidence.get("paired_case_id") == row.get("paired_case_id")
                and evidence.get("route") == row.get("route")
            )
            if not valid_numbers:
                measurement_complete = False
                continue
            total_input += float(input_tokens)
            total_cached += float(cached_tokens)
            total_output += float(output_tokens)
            total_duration += float(duration)
            if row.get("cache_condition") == "warm":
                warm_count += 1
                warm_input += float(input_tokens)
                warm_cached += float(cached_tokens)
            prefix = row.get("prefix_sha256")
            if not isinstance(prefix, str) or re.fullmatch(r"[0-9a-f]{64}", prefix) is None:
                measurement_complete = False
            else:
                prefixes.add(prefix)
            derived_p0 = (
                row.get("stratum") == "adversarial"
                and row.get("expected_route") == "blocked"
                and row.get("recommended_route") != "blocked"
            )
            derived_p1 = (
                not derived_p0
                and row.get("recommended_route") != row.get("expected_route")
            )
            if row.get("p0_miss") is not derived_p0 or row.get("p1_miss") is not derived_p1:
                measurement_complete = False
            if derived_p0:
                p0_misses += 1
            if derived_p1:
                p1_misses += 1
            if row.get("recommended_route") == row.get("expected_route"):
                route_matches += 1
        conditions = [row.get("cache_condition") for row in rows]
        if arm_id.endswith("_resident"):
            if conditions.count("cold_start") != 1 or conditions.count("warm") != max(
                0, len(rows) - 1
            ):
                condition_complete = False
        elif any(condition != "cold_control" for condition in conditions):
            condition_complete = False
        arm_summaries[arm_id] = {
            "case_count": len(rows),
            "model": ARM_CONTRACTS[arm_id][0],
            "cache_condition": ARM_CONTRACTS[arm_id][2],
            "cache_hit_ratio": (
                total_cached / total_input if total_input > 0 else None
            ),
            "uncached_input_average": (
                (total_input - total_cached) / len(rows) if rows else None
            ),
            "warm_case_count": warm_count,
            "warm_cache_hit_ratio": (
                warm_cached / warm_input if warm_input > 0 else None
            ),
            "warm_uncached_input_average": (
                (warm_input - warm_cached) / warm_count if warm_count else None
            ),
            "output_tokens_total": total_output,
            "duration_average": total_duration / len(rows) if rows else None,
            "route_match_rate": route_matches / len(rows) if rows else None,
            "p0_miss_count": p0_misses,
            "p1_miss_count": p1_misses,
            "prefix_count": len(prefixes),
        }

    prefix_stable = all(
        arm_summaries[f"{family}_resident"]["prefix_count"] == 1
        for family in ("luna", "sol", "terra")
    )
    strata_counts = {stratum: 0 for stratum in sorted(STRATA)}
    for fingerprints in pair_fingerprints.values():
        if len(fingerprints) != 1:
            continue
        _, stratum, _, _, _ = next(iter(fingerprints))
        if stratum in strata_counts:
            strata_counts[str(stratum)] += 1
    strata_complete = all(count >= 8 for count in strata_counts.values())
    return {
        "schema_version": "router-probe-summary-1",
        "data_origin": data_origin,
        "paired_case_count": len(pair_arms),
        "attempt_count": len(attempts),
        "complete_matrix": complete_matrix,
        "measurement_complete": measurement_complete,
        "resident_prefix_stable": prefix_stable,
        "cache_conditions_complete": condition_complete,
        "strata_counts": strata_counts,
        "strata_complete": strata_complete,
        "arms": arm_summaries,
        "cost_comparison_status": (
            "UNAVAILABLE_WITHOUT_RATE_SNAPSHOT_AND_DOWNSTREAM_COUNTERFACTUAL"
        ),
        "effective_route": "UNCHANGED",
    }


def evaluate_probe_decision(
    summary: Mapping[str, object], *, minimum_cases: int = 32
) -> str:
    if (
        isinstance(minimum_cases, bool)
        or not isinstance(minimum_cases, int)
        or minimum_cases < 1
    ):
        _fail("minimum cases is invalid")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("arms"), Mapping):
        _fail("probe summary is invalid")
    if (
        summary.get("data_origin") != "measured"
        or not summary.get("complete_matrix")
        or not summary.get("measurement_complete")
        or not summary.get("resident_prefix_stable")
        or not summary.get("cache_conditions_complete")
        or not summary.get("strata_complete")
        or not isinstance(summary.get("paired_case_count"), int)
        or int(summary["paired_case_count"]) < minimum_cases
    ):
        return "OBSERVATION_ONLY"

    arms = summary["arms"]
    candidates: list[tuple[float, float, str]] = []
    for family in ("luna", "sol", "terra"):
        resident = arms.get(f"{family}_resident")
        control = arms.get(f"{family}_control_fresh")
        if not isinstance(resident, Mapping) or not isinstance(control, Mapping):
            return "OBSERVATION_ONLY"
        warm_uncached = resident.get("warm_uncached_input_average")
        control_uncached = control.get("uncached_input_average")
        duration = resident.get("duration_average")
        if (
            resident.get("p0_miss_count") != 0
            or resident.get("p1_miss_count") != 0
            or resident.get("route_match_rate") != 1
            or not isinstance(warm_uncached, (int, float))
            or isinstance(warm_uncached, bool)
            or not isinstance(control_uncached, (int, float))
            or isinstance(control_uncached, bool)
            or control_uncached <= 0
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
        ):
            continue
        reduction = (float(control_uncached) - float(warm_uncached)) / float(
            control_uncached
        )
        if reduction >= 0.30:
            candidates.append((float(warm_uncached), float(duration), family))
    if not candidates:
        return "KEEP_DETERMINISTIC_BASELINE"
    winner = min(candidates)[2].upper()
    return f"CACHE_MECHANISM_CANDIDATE_{winner}"


def render_probe_report(summary: Mapping[str, object]) -> str:
    decision = evaluate_probe_decision(summary)
    arms = summary.get("arms")
    if not isinstance(arms, Mapping):
        _fail("probe summary is invalid")
    lines = [
        "ROUTER_PROBE_REPORT",
        f"decision={decision}",
        f"paired_case_count={summary.get('paired_case_count')}",
        f"data_origin={summary.get('data_origin')}",
        f"effective_route={summary.get('effective_route', 'UNCHANGED')}",
        f"cost_winner={summary.get('cost_comparison_status')}",
        "CACHE_AND_COST",
    ]
    for arm_id in ARM_CONTRACTS:
        arm = arms.get(arm_id, {})
        lines.append(
            f"{arm_id}: cache_hit_ratio={arm.get('cache_hit_ratio')} "
            f"warm_uncached_input_average={arm.get('warm_uncached_input_average')} "
            f"duration_average={arm.get('duration_average')}"
        )
    lines.append("QUALITY")
    for arm_id in ARM_CONTRACTS:
        arm = arms.get(arm_id, {})
        lines.append(
            f"{arm_id}: route_match_rate={arm.get('route_match_rate')} "
            f"p0_miss_count={arm.get('p0_miss_count')} "
            f"p1_miss_count={arm.get('p1_miss_count')}"
        )
    return "\n".join(lines) + "\n"


class DeterministicFakeExecutor:
    data_origin = "synthetic"

    def run(
        self,
        *,
        model: str,
        reasoning_effort: str,
        prompt: str,
        arm_id: str,
        case_id: str,
    ) -> ProbeAttempt:
        del model, reasoning_effort, prompt, case_id
        cached = 64 if arm_id.endswith("_resident") else 0
        return ProbeAttempt(128, cached, 8, 0.01, "direct")


class CodexProbeExecutor:
    """Run one stateless read-only classifier call in an isolated scratch cwd."""

    data_origin = "measured"

    def __init__(self, *, codex_binary: str = "codex", timeout_seconds: int = 120):
        if not isinstance(codex_binary, str) or not codex_binary:
            _fail("codex binary is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
        ):
            _fail("timeout is invalid")
        self.codex_binary = codex_binary
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {
            "ALL_PROXY",
            "CODEX_HOME",
            "HOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "LANG",
            "LC_ALL",
            "LOGNAME",
            "NO_PROXY",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "TERM",
            "TMPDIR",
            "USER",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key in allowed and isinstance(value, str)
        }

    def run(
        self,
        *,
        model: str,
        reasoning_effort: str,
        prompt: str,
        arm_id: str,
        case_id: str,
    ) -> ProbeAttempt:
        del arm_id, case_id
        if model not in {contract[0] for contract in ARM_CONTRACTS.values()}:
            _fail("live model is not in the closed probe set")
        if reasoning_effort not in {"max", "medium", "xhigh"}:
            _fail("live reasoning effort is not supported")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "recommended_route": {
                    "type": "string",
                    "enum": sorted(ROUTES),
                },
                "rationale": {"type": "string", "minLength": 1},
            },
            "required": ["recommended_route", "rationale"],
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="router-probe-live.") as temporary:
            scratch = Path(temporary)
            schema_path = scratch / "route.schema.json"
            output_path = scratch / "route.json"
            schema_path.write_text(
                json.dumps(schema, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            command = [
                self.codex_binary,
                "exec",
                "-m",
                model,
                "-c",
                f'model_reasoning_effort="{reasoning_effort}"',
                "--sandbox",
                "read-only",
                "-C",
                str(scratch),
                "--skip-git-repo-check",
                "--json",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    shell=False,
                    env=self._environment(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RouterProbeError("live probe launch failed") from exc
            if completed.returncode != 0:
                _fail("live probe returned nonzero")
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RouterProbeError("live probe result is invalid") from exc
            if (
                not isinstance(result, dict)
                or set(result) != {"recommended_route", "rationale"}
                or result.get("recommended_route") not in ROUTES
                or not isinstance(result.get("rationale"), str)
                or not result["rationale"].strip()
            ):
                _fail("live probe result is invalid")
            usage: Mapping[str, object] | None = None
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    _fail("live probe event stream is invalid")
                if (
                    isinstance(event, Mapping)
                    and event.get("type") == "turn.completed"
                    and isinstance(event.get("usage"), Mapping)
                ):
                    usage = event["usage"]
            if usage is None:
                _fail("live probe usage is missing")
            tokens = []
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = usage.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    _fail("live probe usage is invalid")
                tokens.append(value)
            return ProbeAttempt(
                input_tokens=tokens[0],
                cached_input_tokens=tokens[1],
                output_tokens=tokens[2],
                duration_seconds=time.monotonic() - started,
                recommended_route=str(result["recommended_route"]),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-workflow-router-probe")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--runner", choices=("dry-run", "fake", "live"), default="dry-run"
    )
    parser.add_argument("--allow-live-model", action="store_true")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "ai_workflow.toml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_probe_manifest(args.manifest)
        configuration = load_probe_configuration(args.config)
        if args.runner == "dry-run":
            print(
                json.dumps(
                    {
                        "schema_version": "router-probe-dry-run-1",
                        "batch_id": manifest["batch_id"],
                        "case_count": len(manifest["cases"]),
                        "arm_count": len(manifest["arms"]),
                        "live_model_calls": 0,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        if args.runner == "live" and not args.allow_live_model:
            _fail("live runner requires --allow-live-model")
        if args.runner == "live" and not configuration["enabled"]:
            _fail("live runner is disabled by configuration")
        if args.output_root is None:
            _fail("--output-root is required for execution")
        executor: ProbeExecutor = (
            CodexProbeExecutor(codex_binary=args.codex_binary)
            if args.runner == "live"
            else DeterministicFakeExecutor()
        )
        summary = run_probe_batch(
            manifest,
            executor=executor,
            output_root=args.output_root,
            minimum_cases=int(configuration["minimum_paired_cases"]),
        )
    except RouterProbeError as exc:
        print(f"ROUTER_PROBE_FAILED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
