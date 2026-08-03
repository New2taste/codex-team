# Trusted Workflow Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, measurable control plane that minimizes unnecessary model delegation, validates bounded plans and runtime identity, packages `luna_worker` reproducibly, and synchronizes the accepted build into the local trial system.

**Architecture:** Preserve `scripts/ai_workflow.py` as the public CLI and existing state-machine authority while moving new responsibilities into focused standard-library modules under `scripts/`. Versioned JSON artifacts feed deterministic routing, stage scheduling, runtime verification, and cost evidence; the Plugin and companion installer distribute the same contracts without pretending that plugin installation natively registers custom agents.

**Tech Stack:** Python 3.11+ standard library, JSON Schema documents, TOML, POSIX shell, Git, `jq`, `shasum`, Codex CLI/desktop custom agents and plugins.

## Global Constraints

- Work only in `/Users/lee/Documents/GPT多模型协作工作流/.worktrees/ai-workflow-experiment` on `codex/ai-workflow-experiment`.
- Do not merge, push, delete a worktree, or broaden sandbox/approval authority.
- Keep `ai-task-1` and `ai-result-1` backward compatible; unknown or conflicting inputs fail closed.
- The existing state machine, owner gates, append-only ledgers, fixed candidate, HEAD/diff/worktree guards, L0/L1/L2 contracts, and retry ceilings remain authoritative.
- Use only Python 3.11+ standard library in runtime code; release scripts may require POSIX shell, Git, `jq`, and SHA256 tools.
- `plugins/ai-workflow/agents/luna-worker.toml` is the release template; `.codex/agents/luna-worker.toml` is a byte-identical project mirror.
- Pin `luna_worker` to `gpt-5.6-luna` with `model_reasoning_effort = "max"`; never silently substitute a built-in worker.
- Record missing token usage as unavailable; never estimate it from bytes, time, role, or model.
- Distinguish `NATIVE_SUBAGENT` from `CODEX_EXEC_ROLE_CONTRACT` in runtime and cost evidence.
- Default routing remains `legacy`; `shadow` records but does not alter execution, and only an owner-approved trial command uses it before `enforced` exists.
- Plugin ID is `ai-workflow@ai-workflow`, Skill name is `$ai-workflow:orchestration`, and first target version is `0.2.0`.
- Initial packaging supports macOS/Linux POSIX environments; Windows support is explicitly unverified.
- Public GitHub publication and license selection are outside this local implementation because the configured `origin` points to an unrelated repository; do not push or reuse that remote.

---

## File Structure

- `scripts/ai_workflow.py`: existing CLI, state machine, runner, store, and compatibility re-exports.
- `scripts/ai_workflow_artifacts.py`: strict loaders, validators, hashes, and dataclasses for new versioned artifacts.
- `scripts/ai_workflow_routing.py`: closed-set route decision and legacy/shadow/enforced policy.
- `scripts/ai_workflow_planning.py`: plan validation, scope ownership, DAG/stage checks, ready batches, dispatch identity.
- `scripts/ai_workflow_runtime.py`: allowlisted runtime observations, identity comparison, evidence writing, JSONL usage extraction.
- `scripts/ai_workflow_costs.py`: attempt cost evidence normalization, pairing, aggregation, and claim gate.
- `config/*.schema.json`: machine-readable contracts for route request/decision, plan, runtime evidence, and cost evidence.
- `tests/test_ai_workflow_*.py`: subsystem tests that import the public CLI module and exercise real behavior.
- `plugins/ai-workflow/`: installable Plugin, Skill, Agent template, scripts, schemas, and runtime entrypoint.
- `.agents/plugins/marketplace.json`: repository-local marketplace catalog.
- `.codex/agents/luna-worker.toml`: project-scoped custom Agent mirror.
- `README.md`: install, invoke, verify, uninstall, safety, platform, and trial-mode instructions.

### Task 1: Versioned Control-Plane Artifact Contracts

**Owner:** `luna_worker` — bounded schema and validator work.

**Files:**
- Create: `scripts/ai_workflow_artifacts.py`
- Create: `config/ai_workflow_route_request.schema.json`
- Create: `config/ai_workflow_route_decision.schema.json`
- Create: `config/ai_workflow_plan.schema.json`
- Create: `config/ai_workflow_runtime_evidence.schema.json`
- Create: `config/ai_workflow_cost_evidence.schema.json`
- Create: `tests/test_ai_workflow_artifacts.py`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Consumes: existing `WorkflowError`, task IDs, risk flags, roles, and canonical JSON behavior.
- Produces: `RouteRequest`, `RouteDecision`, `PlanArtifact`, `RuntimeEvidence`, `CostEvidence`; `load_artifact(path: Path) -> dict[str, object]`; `validate_route_request(value, task)`, `validate_route_decision(value)`, `validate_plan_shape(value)`, `validate_runtime_evidence(value)`, `validate_cost_evidence(value)`; `artifact_sha256(value) -> str`.

- [ ] **Step 1: Write failing schema inventory and strictness tests**

```python
class NewArtifactSchemaTest(unittest.TestCase):
    EXPECTED = {
        "ai_workflow_route_request.schema.json": "ai-route-request-1",
        "ai_workflow_route_decision.schema.json": "ai-route-decision-1",
        "ai_workflow_plan.schema.json": "ai-plan-1",
        "ai_workflow_runtime_evidence.schema.json": "runtime-evidence-1",
        "ai_workflow_cost_evidence.schema.json": "cost-evidence-1",
    }

    def test_every_new_schema_is_strict_and_versioned(self):
        for filename, version in self.EXPECTED.items():
            schema = json.loads((ROOT / "config" / filename).read_text())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(version, schema["properties"]["schema_version"]["const"])
            self.assertEqual(set(schema["properties"]), set(schema["required"]))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_artifacts.NewArtifactSchemaTest -v`

Expected: FAIL because the five schema files do not exist.

- [ ] **Step 3: Add strict schemas with the exact closed sets**

Use these route enums verbatim:

```json
{
  "work_class": ["SIMPLE", "PLANNING_ONLY", "BOUNDED", "MULTI_STAGE", "HIGH_CONSEQUENCE"],
  "execution_need": ["NONE", "READ_ONLY", "WRITE"],
  "route": ["direct", "sol_only", "delegated", "blocked"],
  "routing_mode": ["legacy", "shadow", "enforced"],
  "execution_surface": ["NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"],
  "evidence_class": ["measured", "sample_validated_projection", "unavailable"]
}
```

Every object uses `additionalProperties: false`; arrays that represent sets use `uniqueItems: true`; string identifiers have `minLength: 1`. The plan task fields are exactly `id`, `owner_role`, `read_scope`, `write_scope`, `do_not_touch`, `depends_on`, `expected_result`, `verification_commands`, `first_artifact`, and `evidence_level`.

`runtime-evidence-1.observed_agent_type` has type `["string", "null"]`: it is required and nonempty for `NATIVE_SUBAGENT`, and required with the value `null` for `CODEX_EXEC_ROLE_CONTRACT`.

- [ ] **Step 4: Write failing validator complement tests**

```python
def test_route_request_must_match_task_risk_flags(self):
    request = valid_route_request(risk_flags=[])
    task = valid_task(risk_flags=["SECURITY"])
    with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_CONFLICT"):
        workflow.validate_route_request(request, task)

def test_unknown_artifact_field_is_rejected(self):
    request = valid_route_request()
    request["surprise"] = True
    with self.assertRaisesRegex(workflow.WorkflowError, "UNKNOWN_FIELD"):
        workflow.validate_route_request(request, valid_task())
```

- [ ] **Step 5: Run the validator tests and verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_artifacts -v`

Expected: FAIL because validators are missing.

- [ ] **Step 6: Implement dataclasses, closed-set validation, and canonical hashing**

```python
def artifact_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def load_artifact(path: Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError("ARTIFACT_READ_ERROR", str(exc)) from exc
    if not isinstance(value, dict):
        raise ArtifactError("INVALID_ARTIFACT", "artifact must be an object")
    return value
```

Expose the validators from `scripts/ai_workflow.py` with explicit imports so existing callers keep one public module.

Use a dual import that supports both `from scripts import ai_workflow` and `python3 scripts/ai_workflow.py`:

```python
try:
    from .ai_workflow_artifacts import artifact_sha256, load_artifact
except ImportError:  # direct script execution
    from ai_workflow_artifacts import artifact_sha256, load_artifact
```

- [ ] **Step 7: Run artifact tests and the existing suite**

Run: `python3 -m unittest tests.test_ai_workflow_artifacts -v && python3 -m unittest discover -s tests -v`

Expected: artifact tests pass; all existing 62 tests still pass.

- [ ] **Step 8: Commit the artifact contract**

```bash
git add config scripts/ai_workflow.py scripts/ai_workflow_artifacts.py tests/test_ai_workflow_artifacts.py
git commit -m "feat: define trusted control-plane artifacts"
```

### Task 2: Closed-Set Routing and Shadow Decisions

**Owner:** Terra `xhigh` — policy integration and compatibility.

**Files:**
- Create: `scripts/ai_workflow_routing.py`
- Create: `tests/test_ai_workflow_routing_v2.py`
- Modify: `config/ai_workflow.toml`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Consumes: validated `ai-task-1`, `ai-route-request-1`, `artifact_sha256`, existing legacy `route(task)`.
- Produces: `decide_route(task, request, mode) -> RouteDecision`; `legacy_roles(task) -> tuple[str, ...]`; `record_route_decision(store, task_id, decision) -> Path`; CLI `route --task TASK --request REQUEST --mode legacy|shadow|enforced --root ROOT`.

- [ ] **Step 1: Write route complement and zero-delegation tests**

```python
def test_simple_low_risk_work_routes_direct(self):
    decision = workflow.decide_route(valid_task(), route_request("SIMPLE", "WRITE"), "enforced")
    self.assertEqual("direct", decision.route)
    self.assertEqual((), decision.roles)

def test_planning_only_routes_sol_only_with_zero_workers(self):
    decision = workflow.decide_route(valid_task(), route_request("PLANNING_ONLY", "READ_ONLY"), "enforced")
    self.assertEqual("sol_only", decision.route)
    self.assertEqual(("sol_planner",), decision.roles)

def test_security_write_can_never_route_direct(self):
    task = valid_task(risk_flags=["SECURITY"])
    decision = workflow.decide_route(task, route_request("SIMPLE", "WRITE", ["SECURITY"]), "enforced")
    self.assertEqual("delegated", decision.route)

def test_undecidable_request_fails_closed(self):
    with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_UNDECIDABLE"):
        workflow.decide_route(valid_task(), route_request("HIGH_CONSEQUENCE", "WRITE", decomposable=False), "enforced")
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_routing_v2 -v`

Expected: FAIL because `decide_route` is missing.

- [ ] **Step 3: Implement priority-ordered deterministic rules**

```python
def decide_route(task, request, mode):
    validate_route_request(request, task)
    if mode not in ROUTING_MODES:
        raise RoutingError("ROUTE_INPUT_INVALID", "unknown routing mode")
    if mode == "legacy":
        return decision("delegated", legacy_roles(task), "LEGACY_TASK_TYPE_ROUTE", mode, task, request)
    risky = bool(task["risk_flags"]) or request["work_class"] == "HIGH_CONSEQUENCE"
    if risky and request["execution_need"] == "WRITE" and not request["decomposable"]:
        raise RoutingError("ROUTE_UNDECIDABLE", "high-consequence write lacks bounded decomposition")
    if risky:
        selected = "sol_only" if request["execution_need"] != "WRITE" else "delegated"
    elif request["work_class"] == "PLANNING_ONLY":
        selected = "sol_only"
    elif request["work_class"] == "SIMPLE":
        selected = "direct"
    elif request["work_class"] in {"BOUNDED", "MULTI_STAGE"} and request["decomposable"]:
        selected = "delegated"
    else:
        selected = "blocked"
    return decision(selected, roles_for(selected, task), rule_id_for(selected, risky), mode, task, request)
```

In `shadow`, store the new selected route under `shadow_route` while returning the legacy role chain as `effective_roles`; no existing model-call order changes.

- [ ] **Step 4: Write failing persistence and compatibility tests**

```python
def test_shadow_records_decision_without_changing_legacy_roles(self):
    decision = workflow.decide_route(plan_task(), route_request("SIMPLE", "READ_ONLY"), "shadow")
    self.assertEqual("direct", decision.shadow_route)
    self.assertEqual(("luna", "sol_planner"), decision.effective_roles)

def test_route_decision_binds_both_input_hashes(self):
    decision = workflow.decide_route(valid_task(), route_request("BOUNDED", "WRITE"), "shadow")
    self.assertEqual(workflow.artifact_sha256(valid_task()), decision.task_sha256)
    self.assertEqual(workflow.artifact_sha256(route_request("BOUNDED", "WRITE")), decision.request_sha256)
```

- [ ] **Step 5: Add config and CLI integration**

Add exactly:

```toml
[routing]
mode = "legacy"
```

The `route` CLI validates both files, writes `route-decision.json` atomically under the task directory, appends `ROUTE_DECIDED` with both hashes, and prints canonical JSON. It never starts a model.

- [ ] **Step 6: Run focused and full tests**

Run: `python3 -m unittest tests.test_ai_workflow_routing_v2 -v && python3 -m unittest discover -s tests -v`

Expected: all pass; legacy routing tests are unchanged.

- [ ] **Step 7: Commit routing**

```bash
git add config/ai_workflow.toml scripts/ai_workflow.py scripts/ai_workflow_routing.py tests/test_ai_workflow_routing_v2.py
git commit -m "feat: add shadow-safe deterministic routing"
```

### Task 3: Validated Plans, Ownership, Stages, and Idempotent Dispatch

**Owner:** Terra `xhigh` — graph, scope, and recovery correctness.

**Files:**
- Create: `scripts/ai_workflow_planning.py`
- Create: `tests/test_ai_workflow_planning.py`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Consumes: `ai-plan-1`, parent task, repository-relative allowed paths, append-only store.
- Produces: `validate_plan(plan, task) -> FrozenPlan`; `scope_owner_map(plan) -> dict[str, str]`; `ready_batch(plan, completed, dispatched, capacity) -> tuple[str, ...]`; `dispatch_id(plan_sha256, task_sha256, subtask_id, attempt, candidate) -> str`; `record_dispatch(...)`.

- [ ] **Step 1: Write failing path and ownership tests**

```python
def test_parent_child_write_scopes_overlap(self):
    plan = valid_plan(tasks=[plan_task("a", ["src"]), plan_task("b", ["src/api.py"])])
    with self.assertRaisesRegex(workflow.WorkflowError, "SCOPE_OVERLAP"):
        workflow.validate_plan(plan, remediation_task(["src"]))

def test_absolute_parent_and_glob_paths_are_rejected(self):
    for path in ("/tmp/x", "../x", "src/*.py", ""):
        plan = valid_plan(tasks=[plan_task("a", [path])])
        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task(["src"]))
```

- [ ] **Step 2: Write failing DAG/stage tests**

```python
def test_cycle_is_rejected(self):
    plan = valid_plan(tasks=[plan_task("a", [], ["b"]), plan_task("b", [], ["a"])], stages=[["a"], ["b"]])
    with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_CYCLE"):
        workflow.validate_plan(plan, valid_task())

def test_dependency_must_be_in_an_earlier_stage(self):
    plan = valid_plan(tasks=[plan_task("a"), plan_task("b", depends_on=["a"])], stages=[["a", "b"]])
    with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
        workflow.validate_plan(plan, valid_task())
```

- [ ] **Step 3: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_planning -v`

Expected: FAIL because planning functions are missing.

- [ ] **Step 4: Implement normalized prefix-aware scope validation**

```python
def normalize_scope(path: str) -> PurePosixPath:
    if not path or path.startswith("/") or any(char in path for char in "*?[]"):
        raise PlanningError("PLAN_INVALID", "scope must be a literal repository-relative path")
    value = PurePosixPath(path)
    if ".." in value.parts or "." in value.parts:
        raise PlanningError("PLAN_INVALID", "scope cannot traverse")
    return value

def scopes_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    return left == right or left in right.parents or right in left.parents
```

Treat subtask `id` as the ownership identity. Reusing an `owner_role` is legal; assigning overlapping paths to different task IDs is not.

- [ ] **Step 5: Implement topological stage validation and capacity batches**

```python
def ready_batch(plan, completed, dispatched, capacity):
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise PlanningError("CAPACITY_UNAVAILABLE", "capacity must be a non-negative integer")
    current = first_incomplete_stage(plan, completed)
    ready = sorted(
        task["id"] for task in tasks_in_stage(plan, current)
        if set(task["depends_on"]) <= set(completed) and task["id"] not in dispatched
    )
    return tuple(ready[:capacity])
```

Capacity zero returns an empty batch plus a `CAPACITY_UNAVAILABLE` event at the orchestration layer; it never rewrites stages or owners.

- [ ] **Step 6: Write and implement idempotent dispatch tests**

```python
def test_same_dispatch_identity_is_not_launched_twice(self):
    identity = workflow.dispatch_id("p" * 64, "t" * 64, "task-a", 1, "c" * 40)
    store.record_dispatch(task_id, identity, payload)
    with self.assertRaisesRegex(workflow.WorkflowError, "DUPLICATE_DISPATCH"):
        store.record_dispatch(task_id, identity, payload)
```

Build the ID as SHA256 of canonical JSON containing exactly `plan_sha256`, `task_sha256`, `subtask_id`, `attempt`, and `candidate_commit`.

- [ ] **Step 7: Run planning and full regression tests**

Run: `python3 -m unittest tests.test_ai_workflow_planning -v && python3 -m unittest discover -s tests -v`

Expected: all pass.

- [ ] **Step 8: Commit planning and scheduling**

```bash
git add scripts/ai_workflow.py scripts/ai_workflow_planning.py tests/test_ai_workflow_planning.py
git commit -m "feat: validate bounded stage scheduling"
```

### Task 4: Runtime Identity, Permissions, and Usage Evidence

**Owner:** Terra `xhigh` — security-sensitive runtime integration.

**Files:**
- Create: `scripts/ai_workflow_runtime.py`
- Create: `plugins/ai-workflow/scripts/inspect-agent-runtime.sh`
- Create: `tests/test_ai_workflow_runtime.py`
- Create: `tests/fixtures/runtime/`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Consumes: requested role contract, Codex JSONL, native metadata or a uniquely matched rollout.
- Produces: `RuntimeObservation`; `verify_runtime_identity(requested, observed) -> RuntimeEvidence`; `extract_codex_usage(events) -> dict[str, int | None]`; `write_runtime_evidence(store, task_id, evidence)`; allowlisted inspector JSON.

- [ ] **Step 1: Write failing identity complement tests**

```python
def test_every_native_identity_field_is_required(self):
    for field in ("agent_type", "model", "reasoning_effort", "sandbox_policy", "permission_profile", "cwd"):
        observed = valid_observation()
        observed[field] = None
        with self.subTest(field=field), self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_MISSING"):
            workflow.verify_runtime_identity(expected_luna(), observed)

def test_exec_surface_must_not_claim_a_custom_agent_type(self):
    observed = valid_observation(surface="CODEX_EXEC_ROLE_CONTRACT", agent_type="luna_worker")
    with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
        workflow.verify_runtime_identity(expected_exec_luna(), observed)

def test_conflicting_public_and_rollout_values_fail(self):
    with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
        workflow.merge_runtime_observations(valid_observation(model="gpt-5.6-luna"), valid_observation(model="gpt-5.6-sol"))
```

- [ ] **Step 2: Write failing usage extraction test from the real JSONL shape**

```python
def test_turn_completed_usage_is_recorded_without_estimation(self):
    events = [
        {"type": "thread.started", "thread_id": "019fc73c-4d40-7c20-a82a-c5a9ae078bcf"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2}},
    ]
    self.assertEqual(
        {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2},
        workflow.extract_codex_usage(events),
    )

def test_missing_usage_stays_null(self):
    self.assertEqual(
        {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None},
        workflow.extract_codex_usage([{"type": "turn.completed"}]),
    )
```

- [ ] **Step 3: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_runtime -v`

Expected: FAIL because runtime verification functions are missing.

- [ ] **Step 4: Implement exact comparison and behaviorally read-only rule**

```python
def verify_runtime_identity(expected, observed):
    required = ("model", "reasoning_effort", "sandbox_policy", "permission_profile", "cwd")
    missing = [name for name in required if not observed.get(name)]
    if missing:
        raise RuntimeIdentityError("RUNTIME_IDENTITY_MISSING", missing[0])
    if observed["execution_surface"] == "NATIVE_SUBAGENT" and not observed.get("agent_type"):
        raise RuntimeIdentityError("RUNTIME_IDENTITY_MISSING", "agent_type")
    if observed["execution_surface"] == "CODEX_EXEC_ROLE_CONTRACT" and observed.get("agent_type") is not None:
        raise RuntimeIdentityError("RUNTIME_IDENTITY_CONFLICT", "exec is not a custom agent")
    compared = ("model", "reasoning_effort", "cwd")
    mismatches = [name for name in compared if observed[name] != expected[name]]
    if observed["execution_surface"] == "NATIVE_SUBAGENT" and observed["agent_type"] != expected["agent_type"]:
        mismatches.append("agent_type")
    if mismatches:
        raise RuntimeIdentityError("RUNTIME_IDENTITY_CONFLICT", mismatches[0])
    if not permission_is_within_contract(expected, observed):
        raise RuntimeIdentityError("RUNTIME_PERMISSION_MISMATCH", "effective permission exceeds contract")
    return runtime_evidence(expected, observed, "VERIFIED", [])
```

A broadened reviewer sandbox can pass only when `hard_read_only` is false, the prompt forbids writes, and exact before/after repository and artifact snapshots match; otherwise fail.

- [ ] **Step 5: Implement the allowlisted rollout inspector**

The shell script accepts `--sessions-dir ABSOLUTE_DIR THREAD_ID`, validates the UUID, finds exactly one rollout filename ending in that ID, and uses `jq` to emit only:

```json
{"thread_id":"...","agent_type":"...","model":"...","reasoning_effort":"...","sandbox_policy":"...","permission_profile":"...","cwd":"..."}
```

It must reject zero/multiple matches, nonregular files, inconsistent duplicate fields, missing fields, relative session roots, and any output key outside the allowlist. It never prints prompts, messages, environment variables, tokens, config contents, or arbitrary payloads.

- [ ] **Step 6: Add fixture tests for inspector privacy and uniqueness**

Run the script against disposable fixtures containing sentinel values `PROMPT_SECRET`, `ENV_SECRET`, and `TOKEN_SECRET`; assert none appear in stdout or stderr. Create zero-match and two-match cases and assert nonzero exit.

- [ ] **Step 7: Integrate automatic execution surface evidence**

For live CLI runs, record `execution_surface = "CODEX_EXEC_ROLE_CONTRACT"`, requested model/effort/sandbox/cwd, thread ID from JSONL, exact usage when present, and the inspector result when available. Do not label it `NATIVE_SUBAGENT`. Reject stale/missing runtime evidence before promoting a role result to the canonical output.

- [ ] **Step 8: Run runtime, security, and full tests**

Run: `python3 -m unittest tests.test_ai_workflow_runtime -v && sh -n plugins/ai-workflow/scripts/inspect-agent-runtime.sh && python3 -m unittest discover -s tests -v`

Expected: all pass; sentinel secrets absent.

- [ ] **Step 9: Commit runtime evidence**

```bash
git add scripts/ai_workflow.py scripts/ai_workflow_runtime.py plugins/ai-workflow/scripts/inspect-agent-runtime.sh tests/test_ai_workflow_runtime.py tests/fixtures/runtime
git commit -m "feat: verify workflow runtime identity"
```

### Task 5: Cost Evidence, Pairing, and Claim Gates

**Owner:** `luna_worker` — bounded numeric normalization and report tests.

**Files:**
- Create: `scripts/ai_workflow_costs.py`
- Create: `tests/test_ai_workflow_costs.py`
- Create: `tests/fixtures/paired-cases.json`
- Modify: `scripts/ai_workflow.py`

**Interfaces:**
- Consumes: role attempts, route decisions, runtime usage, durations, quality outcomes.
- Produces: `normalize_cost_evidence(value) -> CostEvidence`; `aggregate_paired_cases(records) -> dict`; `evaluate_cost_claim(summary, minimum_cases=30, quality_margin_points=5.0) -> str`; report sections for measured/projected/unavailable.

- [ ] **Step 1: Write failing unavailable and invalid-number tests**

```python
def test_missing_tokens_remain_unavailable(self):
    evidence = workflow.normalize_cost_evidence(cost_record(input_tokens=None, output_tokens=None))
    self.assertEqual("unavailable", evidence.evidence_class)
    self.assertIsNone(evidence.input_tokens)

def test_invalid_numbers_are_rejected(self):
    for value in (-1, True, float("nan"), "100"):
        with self.subTest(value=value), self.assertRaisesRegex(workflow.WorkflowError, "COST_EVIDENCE_INVALID"):
            workflow.normalize_cost_evidence(cost_record(input_tokens=value))
```

- [ ] **Step 2: Write failing overhead and execution-surface tests**

```python
def test_failed_and_retry_attempts_count_in_the_same_pair(self):
    summary = workflow.aggregate_paired_cases([
        cost_record(pair="case-01", role="sol_planner", status="FAILED", input_tokens=10),
        cost_record(pair="case-01", role="sol_planner", retry_kind="technical", input_tokens=12),
        cost_record(pair="case-01", role="luna", surface="NATIVE_SUBAGENT", input_tokens=5),
    ])
    self.assertEqual(27, summary["case-01"]["measured_input_tokens"])
    self.assertEqual(1, summary["case-01"]["technical_retries"])
    self.assertEqual({"NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"}, set(summary["case-01"]["surfaces"]))
```

The fixture must include at least one record for each surface; do not synthesize a missing surface.

- [ ] **Step 3: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_costs -v`

Expected: FAIL because the cost module is missing.

- [ ] **Step 4: Implement normalization and evidence separation**

```python
def finite_nonnegative_or_none(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise CostError("COST_EVIDENCE_INVALID", field)
    return value
```

`measured` rows require at least one explicit measured usage or duration field. `sample_validated_projection` requires a nonempty `rate_snapshot_id`; `unavailable` cannot contain projected price values. Store raw attempts append-only and derive summaries without rewriting them.

- [ ] **Step 5: Add the 30-case claim gate**

Create `tests/fixtures/paired-cases.json` with stable IDs `case-01` through `case-30`, stratified across `direct`, `sol_only`, and `delegated`. Fixture values test the gate only; label them synthetic and never publish them as experiment results.

```python
def evaluate_cost_claim(summary, minimum_cases=30, quality_margin_points=5.0):
    if summary["paired_case_count"] < minimum_cases:
        return "OBSERVATION_ONLY"
    if summary["quality_delta_points"] < -quality_margin_points:
        return "QUALITY_REGRESSION"
    if summary["net_measured_cost_delta"] is None or summary["net_measured_cost_delta"] >= 0:
        return "NO_COST_REDUCTION_PROVEN"
    return "COST_REDUCTION_SUPPORTED"
```

- [ ] **Step 6: Extend the report without weakening the calibration disclaimer**

Render separate `Measured`, `Projection`, and `Unavailable` sections; include route, execution surface, retry overhead, prompt bytes, and paired-case count. The report may say `COST_REDUCTION_SUPPORTED` only when the deterministic gate returns it.

- [ ] **Step 7: Run cost and full tests**

Run: `python3 -m unittest tests.test_ai_workflow_costs -v && python3 -m unittest discover -s tests -v`

Expected: all pass; existing report disclaimer remains present.

- [ ] **Step 8: Commit cost evidence**

```bash
git add scripts/ai_workflow.py scripts/ai_workflow_costs.py tests/test_ai_workflow_costs.py tests/fixtures/paired-cases.json
git commit -m "feat: account for workflow routing costs"
```

### Task 6: Reproducible Luna Agent and Plugin Lifecycle

**Owner:** Terra `xhigh` — installer safety, packaging, and migration.

**Files:**
- Create: `.codex/agents/luna-worker.toml`
- Create: `.agents/plugins/marketplace.json`
- Create: `plugins/ai-workflow/.codex-plugin/plugin.json`
- Create: `plugins/ai-workflow/agents/luna-worker.toml`
- Create: `plugins/ai-workflow/scripts/install-agents.sh`
- Create: `plugins/ai-workflow/scripts/uninstall-agents.sh`
- Create: `plugins/ai-workflow/scripts/verify.sh`
- Create: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Create: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Create: `plugins/ai-workflow/config/ai_workflow.toml`
- Create: `plugins/ai-workflow/config/ai_workflow_task.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_result.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_route_request.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_route_decision.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_plan.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_runtime_evidence.schema.json`
- Create: `plugins/ai-workflow/config/ai_workflow_cost_evidence.schema.json`
- Create: `plugins/ai-workflow/runtime/ai_workflow.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_artifacts.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_routing.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_planning.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_runtime.py`
- Create: `plugins/ai-workflow/runtime/ai_workflow_costs.py`
- Create: `tests/test_ai_workflow_distribution.py`

**Interfaces:**
- Consumes: current verified global Agent digest `60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024`, repository contracts, plugin CLI.
- Produces: project Agent discovery; Plugin `ai-workflow@ai-workflow`; installer `--target-dir PATH` and `--check`; safe uninstaller `--target-dir PATH`; isolated verifier.

- [ ] **Step 1: Write failing distribution inventory and exactness tests**

```python
def test_plugin_manifest_and_marketplace_are_versioned(self):
    manifest = json.loads((ROOT / "plugins/ai-workflow/.codex-plugin/plugin.json").read_text())
    self.assertEqual("ai-workflow", manifest["name"])
    self.assertEqual("0.2.0", manifest["version"])
    self.assertEqual("./skills/", manifest["skills"])

def test_project_and_release_agent_templates_are_byte_exact(self):
    self.assertEqual(
        (ROOT / ".codex/agents/luna-worker.toml").read_bytes(),
        (ROOT / "plugins/ai-workflow/agents/luna-worker.toml").read_bytes(),
    )
```

- [ ] **Step 2: Write failing Agent contract test**

```python
def test_luna_template_matches_the_project_contract(self):
    with (ROOT / "plugins/ai-workflow/agents/luna-worker.toml").open("rb") as handle:
        agent = tomllib.load(handle)
    self.assertEqual("luna_worker", agent["name"])
    self.assertEqual("gpt-5.6-luna", agent["model"])
    self.assertEqual("max", agent["model_reasoning_effort"])
    self.assertIn("L0/L1/L2", agent["developer_instructions"])
    self.assertNotIn("ACCEPTED", agent["developer_instructions"])
```

- [ ] **Step 3: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_distribution -v`

Expected: FAIL because packaging files are missing.

- [ ] **Step 4: Add the exact Agent template and plugin metadata**

Copy the verified behavior contract into both Agent files with these immutable identity fields:

```toml
name = "luna_worker"
model = "gpt-5.6-luna"
model_reasoning_effort = "max"
```

The manifest is:

```json
{
  "name": "ai-workflow",
  "version": "0.2.0",
  "description": "Deterministic multi-model routing, evidence, and review workflow for Codex.",
  "skills": "./skills/",
  "interface": {
    "displayName": "AI Workflow",
    "shortDescription": "Route bounded Codex work with evidence gates",
    "category": "Developer Tools",
    "capabilities": ["Read", "Write"],
    "defaultPrompt": ["Use $ai-workflow:orchestration to route and execute this task with evidence gates."]
  }
}
```

Do not add a license or repository URL until the owner selects the actual open-source repository; local packaging must not point at the unrelated current `origin`.

Add a valid initial Skill that performs installer `--check`, explains the native/exec distinction, requires exact `luna_worker`, preserves owner gates, and stops after preflight. Task 7 extends this same file with live control-plane execution; Task 6 never ships a manifest pointing at a missing skill.

- [ ] **Step 5: Write installer lifecycle tests before the scripts**

In a `TemporaryDirectory`, exercise `missing`, `current`, `known_legacy`, `conflict`, `unsafe`, and `unreadable`. Required assertions:

```python
self.assertEqual(0, install(target))
self.assertEqual(template_bytes, destination.read_bytes())
self.assertEqual(0, install(target, check=True))
self.assertEqual(before_hashes, unrelated_hashes(target))
self.assertNotEqual(0, install(conflicting_target))
self.assertEqual(conflicting_bytes, destination.read_bytes())
```

The `known_legacy` set contains the verified digest above for the first release. Symlink destinations and symlink target directories fail without mutation.

- [ ] **Step 6: Implement preflight-first atomic installation**

`install-agents.sh` uses `set -eu`, resolves an explicit target without `eval`, refuses `/` and empty paths, classifies every destination before mutation, stages in the destination directory, and atomically moves only after rechecking state. `--check` performs no `mkdir`, `cp`, `mv`, `ln`, or `rm`.

The installed state file contains only plugin version, target filename, installed SHA256, UTC timestamp, and optional backup SHA256. It contains no prompts, credentials, home-directory inventory, or unrelated Agent details.

- [ ] **Step 7: Implement ownership-safe uninstall**

`uninstall-agents.sh` removes `luna-worker.toml` only when the state record names it and its current SHA256 equals the recorded installed SHA256. A modified file is preserved with nonzero exit. No recursive deletion is allowed. Restore a backup only when its recorded hash matches and the current target state permits replacement.

- [ ] **Step 8: Package runtime and schemas without divergent copies**

`verify.sh` compares every listed plugin config file with `config/` and every listed runtime module with `scripts/` byte-for-byte. Use a deterministic sync command during implementation, then commit both copies; the verifier, not developer memory, enforces parity.

- [ ] **Step 9: Run distribution verifier and lifecycle tests**

Run: `python3 -m unittest tests.test_ai_workflow_distribution -v && sh plugins/ai-workflow/scripts/verify.sh && git diff --check`

Expected: all pass in disposable targets; no global Codex files changed.

- [ ] **Step 10: Commit packaging**

```bash
git add .agents .codex plugins/ai-workflow tests/test_ai_workflow_distribution.py
git commit -m "feat: package the Luna workflow plugin"
```

### Task 7: Orchestration Skill, CLI Integration, and User Documentation

**Owner:** Terra `xhigh` — cross-component integration.

**Files:**
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `plugins/ai-workflow/skills/orchestration/agents/openai.yaml`
- Create: `tests/test_ai_workflow_integration_v2.py`
- Modify: `scripts/ai_workflow.py`
- Modify: `README.md`
- Modify: `plugins/ai-workflow/runtime/ai_workflow.py`

**Interfaces:**
- Consumes: route decision, plan validation, ready batches, runtime and cost evidence, Plugin preflight.
- Produces: explicit `$ai-workflow:orchestration` workflow; CLI `route`, `plan`, `dispatch-preview`, and shadow-aware `run`; user-facing installation and invocation contract.

- [ ] **Step 1: Write failing CLI path tests**

```python
def test_direct_route_makes_zero_runner_calls(self):
    runner = ScriptedRunner([])
    result = workflow.run_control_plane(task, route_request("SIMPLE", "WRITE"), runner=runner, mode="enforced")
    self.assertEqual("DIRECT_HANDOFF", result.status)
    self.assertEqual([], runner.calls)

def test_sol_only_makes_zero_worker_calls(self):
    runner = ScriptedRunner([sol_plan_result()])
    workflow.run_control_plane(task, route_request("PLANNING_ONLY", "READ_ONLY"), runner=runner, mode="enforced")
    self.assertEqual(["sol_planner"], runner.calls)

def test_plugin_without_companion_agent_fails_closed(self):
    with self.assertRaisesRegex(workflow.WorkflowError, "LUNA_AGENT_NOT_INSTALLED"):
        workflow.preflight_native_agent(empty_agents_dir, "luna_worker")
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.test_ai_workflow_integration_v2 -v`

Expected: FAIL because control-plane integration is missing.

- [ ] **Step 3: Implement control-plane entry without creating a second state machine**

```python
def run_control_plane(task, route_request, *, runner, mode, store):
    decision = decide_route(task, route_request, mode)
    record_route_decision(store, task["task_id"], decision)
    if decision.effective_route == "direct":
        return ControlPlaneResult("DIRECT_HANDOFF", ())
    if decision.effective_route == "blocked":
        return ControlPlaneResult("BLOCKED", ())
    if decision.effective_route == "sol_only":
        return run_existing_state_machine(task, runner=runner, role_filter={"sol_planner", "sol_reviewer"})
    plan = load_and_validate_pinned_plan(store, task)
    return dispatch_plan_through_existing_state_machine(task, plan, runner, store)
```

Route/plan artifacts control entry and scheduling only; existing state transitions and owner decisions remain the sole execution truth.

- [ ] **Step 4: Write the Skill with fail-closed preflight and explicit surfaces**

The Skill must:

- validate the installed Agent template with `install-agents.sh --check` before native delegation;
- require exact `agent_type: luna_worker` for native Luna tasks;
- prohibit fallback to `worker`, `default`, Terra, or a plain Luna model when the native path was requested;
- use the five-part task packet from the approved plan and return only bounded evidence;
- tell users that automatic CLI uses `CODEX_EXEC_ROLE_CONTRACT`, not native `luna_worker`;
- preserve owner gates and forbid merge, push, worktree deletion, or self-approval;
- require controller diff/tests and fresh Sol final review before completion.

- [ ] **Step 5: Add CLI commands and exact exit behavior**

Add:

```text
route --task TASK --request REQUEST --mode legacy|shadow|enforced --root ROOT
plan --task TASK --plan PLAN --root ROOT
dispatch-preview --task TASK --plan PLAN --capacity N --root ROOT
```

`direct` prints `DIRECT_HANDOFF` and exits 0 without a model call. `blocked` prints the closed error code and exits 2. `dispatch-preview` is read-only and never spawns workers.

- [ ] **Step 6: Update README with tested install and invoke commands**

Document exact local commands:

```bash
codex plugin marketplace add .
codex plugin add ai-workflow@ai-workflow
plugin_dir="$(codex plugin list --json | jq -r '.installed[] | select(.pluginId == "ai-workflow@ai-workflow") | .installedPath')"
test -n "$plugin_dir" && test -d "$plugin_dir"
sh "$plugin_dir/scripts/install-agents.sh"
sh "$plugin_dir/scripts/install-agents.sh" --check
```

Document the interactive prompt `使用 $ai-workflow:orchestration 执行这个任务，按可信控制平面路由。`, the direct `luna_worker` request, automated CLI commands, restart/new-task requirement, uninstall safety, macOS/Linux support, Windows unverified status, and the unrelated-origin publication stop line.

- [ ] **Step 7: Run fake four-path closure and full tests**

Run: `python3 -m unittest tests.test_ai_workflow_integration_v2 -v && python3 -m unittest discover -s tests -v && python3 -m compileall -q scripts tests`

Expected: direct, sol-only, delegated, and blocked paths pass without live model use; all legacy tests remain green.

- [ ] **Step 8: Commit integration**

```bash
git add README.md scripts/ai_workflow.py plugins/ai-workflow tests/test_ai_workflow_integration_v2.py
git commit -m "feat: integrate the trusted workflow control plane"
```

### Task 8: Trial-System Synchronization

**Owner:** Primary controller — external user-owned configuration is not delegated.

**Files:**
- Runtime target: a new temporary `CODEX_HOME`
- Runtime target: `/Users/lee/.codex/agents/luna-worker.toml`
- Runtime target: local Codex marketplace/plugin configuration
- Evidence only: ignored `data/state/ai-workflow/trial-sync/`

**Interfaces:**
- Consumes: committed Plugin candidate and installer/verifier from Task 7.
- Produces: reproducible temporary install evidence, current local Plugin install, verified global Luna Agent, and a restart/new-task handoff. No repository commit is required unless a defect is found.

- [ ] **Step 1: Verify the candidate is clean before external synchronization**

Run: `git status --short && git rev-parse HEAD && sh plugins/ai-workflow/scripts/verify.sh`

Expected: empty status, one candidate SHA, verifier passes.

- [ ] **Step 2: Exercise the complete lifecycle in an explicit temporary home**

```bash
trial_codex_home="$(mktemp -d /private/tmp/ai-workflow-codex-home.XXXXXX)"
sh plugins/ai-workflow/scripts/install-agents.sh --target-dir "$trial_codex_home/agents"
sh plugins/ai-workflow/scripts/install-agents.sh --target-dir "$trial_codex_home/agents" --check
sh plugins/ai-workflow/scripts/uninstall-agents.sh --target-dir "$trial_codex_home/agents"
```

Expected: install/check/uninstall pass; only the explicit temporary directory changes. Preserve command outputs in the ignored trial-sync evidence directory; do not commit machine paths.

- [ ] **Step 3: Add and install the local marketplace Plugin**

Run from the worktree root:

```bash
codex plugin marketplace add .
codex plugin add ai-workflow@ai-workflow
codex plugin list --json
```

Expected: installed plugin ID is exactly `ai-workflow@ai-workflow`, enabled, version `0.2.0`, and its installed path resolves inside Codex's plugin cache. Do not remove or modify unrelated marketplaces/plugins.

- [ ] **Step 4: Synchronize the global Luna Agent through the installer**

Resolve the installed plugin directory from `codex plugin list --json`, then run its installer without a custom target and run `--check`. The current verified digest should classify as `current`; if it classifies as conflict or unsafe, stop and preserve the user's file.

Expected final hash:

```text
60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024
```

- [ ] **Step 5: Validate Codex configuration and discovery boundary**

Run: `codex --strict-config --version` and parse the installed TOML with Python 3.11+. Confirm `name=luna_worker`, `model=gpt-5.6-luna`, and `model_reasoning_effort=max`. Record that existing tasks may retain old discovery and a new Codex task is required; do not restart or terminate the user's active app automatically.

- [ ] **Step 6: Run one shadow decision in the trial system**

Use a simple read-only fixture task and `route --mode shadow`. Assert that the recorded `shadow_route` is `direct` while `effective_roles` remain the legacy chain, and that zero extra live model calls occur for the routing decision.

### Task 9: Independent Verification, Mutations, Live Luna, and Final Review

**Owner:** Primary controller for verification; fresh Sol `xhigh` for whole-branch review.

**Files:**
- Review: all tracked files since `dec50eb`
- Runtime only: ignored workflow state and trial-sync evidence

**Interfaces:**
- Consumes: Tasks 1–8 candidate, installed trial Plugin/Agent, full test evidence.
- Produces: final clean candidate commit and Sol verdict; no merge, push, or worktree deletion.

- [ ] **Step 1: Run the complete deterministic verification gate**

```bash
git status --short
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
sh plugins/ai-workflow/scripts/verify.sh
git diff --check
```

Expected: clean before tests; all commands exit 0; tests do not dirty tracked files.

- [ ] **Step 2: Run eight high-value mutations one at a time**

Each mutation must make its named test fail, then be restored before the next:

1. Allow a `SECURITY` task to route direct.
2. Make `shadow` use the new roles instead of legacy effective roles.
3. Disable prefix-aware scope overlap detection.
4. Permit the same dispatch ID twice.
5. Accept missing runtime model evidence.
6. Treat missing tokens as zero measured tokens.
7. Let installer overwrite an unknown conflicting Agent.
8. Let Plugin preflight fall back from `luna_worker` to `worker`.

After restoration, rerun the focused test and confirm green. No mutation commit is allowed.

- [ ] **Step 3: Verify the Plugin and installed Agent identities**

Run the repository verifier, installed-plugin verifier, global installer `--check`, TOML parse, and SHA256. Confirm project mirror, release template, cached plugin template, and global Agent are identical.

- [ ] **Step 4: Run a real native `luna_worker` read-only smoke in a new task context**

Delegate one L1 audit of the new route/plan schemas with no write authorization. Capture public spawn metadata and, if needed, the allowlisted rollout inspector output. Require `NATIVE_SUBAGENT`, exact Luna/Max, one counter-check, at most five claims, no changed files, and no final acceptance status.

- [ ] **Step 5: Run a real automatic Luna smoke separately**

Use `python3 scripts/ai_workflow.py run --runner live --allow-live-model --role luna` on a bounded L1 task. Require `CODEX_EXEC_ROLE_CONTRACT`, exact measured usage when emitted, fixed output attempt, read-only diff, and no Sol/Terra calls. Do not describe it as native `luna_worker`.

- [ ] **Step 6: Dispatch a fresh whole-branch Sol review**

Provide the approved spec, this plan, base commit `dec50eb`, candidate commit, full diff package, verification output, mutation results, temporary lifecycle evidence, installed Plugin/Agent hashes, and both Luna smoke artifacts. Sol returns exactly `ACCEPTANCE_RECOMMENDED`, `REWORK_RECOMMENDED`, or `RETHINK_RECOMMENDED` and remains read-only.

- [ ] **Step 7: Allow at most one final fix wave and one scoped re-review**

If Sol finds load-bearing defects, dispatch one Terra `xhigh` fix wave with exact file ownership, rerun affected tests plus the full gate, and request one scoped Sol re-review. Adjudicate residual non-load-bearing observations explicitly; do not loop indefinitely.

- [ ] **Step 8: Commit the final accepted candidate**

```bash
git add README.md config scripts tests plugins .agents .codex docs/superpowers/plans/2026-08-03-trusted-control-plane.md
git commit -m "feat: deliver trusted workflow control plane"
```

Expected: final worktree clean. Do not merge, push, or delete the worktree.

## Plan Self-Review Results

- **Spec coverage:** route request/decision, four paths, shadow compatibility, plan artifact, owner/scope/DAG/capacity, dispatch idempotency, runtime identity, usage parsing, cost evidence, Plugin/Agent lifecycle, invocation, trial synchronization, failure injection, and staged activation all map to Tasks 1–9.
- **Safety coverage:** existing owner, state, Git, evidence, retry, secret, and candidate protections are preserved and re-run in every full gate; packaging operates in temporary homes before the explicitly authorized global sync.
- **Type consistency:** `RouteRequest`, `RouteDecision`, `PlanArtifact`, `RuntimeEvidence`, `CostEvidence`, `execution_surface`, `routing_mode`, and artifact version strings use the same names in schemas, modules, tests, CLI, Plugin, and report.
- **No hidden activation:** default remains `legacy`; the synchronized trial executes only a shadow decision. `enforced` is implemented and tested but is not made the local default without later owner evidence.
- **No false identity:** native custom-agent and automatic `codex exec` paths are independently verified and independently metered.
- **No publication accident:** the unrelated configured Git remote is never pushed; universal directory publication and license selection remain an explicit later owner decision.
