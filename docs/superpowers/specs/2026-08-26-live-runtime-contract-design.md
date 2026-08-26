# Live Runtime Contract Repair Design

## Goal

Make the existing live Luna path reach its current result-ingestion boundary
without weakening runtime identity checks or scheduler identity checks.

## S3: Real Codex rollout inspection

Codex CLI `0.150.0-alpha.8` emits JSONL. Runtime identity fields are spread
across multiple records rather than repeated in one synthetic object:

- session identity is recorded as `payload.id` / `payload.session_id`;
- model and reasoning effort occur in rollout records;
- `sandbox_policy` is an object such as `{"type":"read-only"}`;
- `permission_profile` is an object whose managed restricted-filesystem shape
  must normalize to the existing `read-only` contract.

The inspector must parse the complete JSONL stream as one array, collect each
required identity field across all records, and require exactly one normalized
value. Missing, malformed, conflicting, or unknown values fail closed with the
existing generic `runtime inspection failed` diagnostic. It must retain the
unique regular-file, no-symlink, absolute-directory, UUID, and final output
allowlist guards.

A redacted real-rollout fixture, captured from Codex CLI
`0.150.0-alpha.8` on 2026-08-26, is the authoritative positive fixture.
Synthetic negative fixtures are derived from it.

## S4: Bound task-id echo normalization

Provider-strict output forces all four identity keys onto live results. A
model that sees the task envelope may honestly return:

```json
{
  "dispatch_id": null,
  "task_id": "<bound task id>",
  "step_id": null,
  "attempt": null
}
```

This is accepted as an identity echo only when the controller supplies a
non-empty `expected_task_id` and the returned `task_id` exactly equals it.
The normalization removes all four keys on a copy. It never rewrites the raw
attempt artifact.

All other partially-null shapes remain invalid. In particular:

- a different task id is rejected;
- any non-null dispatch id, step id, or attempt is rejected;
- a missing/invalid expected task id does not enable echo normalization;
- the scheduler path is unchanged and still rejects null identity before
  generic result validation.

Both normal role ingestion and repairs controller ingestion use the same
closed normalization rule. Prompts instruct models to output all four
identity fields as null, reducing but not relying on model compliance.

## Joint acceptance gate

After focused and full tests pass, run the same real Team Call L1 Luna probe.
Acceptance requires:

1. provider schema validation succeeds;
2. runtime rollout inspection succeeds;
3. the bound task-id echo is accepted without mutating the raw attempt file;
4. command exit code is zero;
5. the repository remains clean and no config/plugin artifact is modified by
   the live run.

Git read operations may atomically refresh the index with byte-identical
content and leave only inode/time metadata drift. The control-plane snapshot
therefore defines “unchanged” as no durable path, mode, size, or content
change. Persistent additions, deletions, permission changes, ref changes, or
content changes still fail closed. A mutate-then-restore action entirely
inside the L1 window is outside this after-the-fact snapshot guarantee; the
read-only sandbox and verified runtime permission remain the primary boundary.

## Non-goals

- No weakening of runtime provenance or permission checks.
- No scheduler behavior change.
- No general acceptance of partially-null identity.
- No router research integration.
