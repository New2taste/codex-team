# Luna Max Agent Rename Design

## Goal

Replace the project custom-agent identifier `luna_worker` with `luna_max` and
the release/project template filename `luna-worker.toml` with
`luna-max.toml`.  The human-facing name is **Luna Max**.  The model remains
`gpt-5.6-luna` at reasoning effort `max`.

## Scope

This is an identifier migration, not a role-policy change.  Luna Max remains a
bounded construction role: it may work only under an exact, verified
construction envelope and may never review, approve, or provide final
acceptance.  The Terra/Sol allocation, retry ladder, cost accounting, and
automatic merge/push prohibitions remain unchanged.

## Canonical identifiers

| Concern | Old value | Canonical value after migration |
|---|---|---|
| Custom-agent machine identifier | `luna_worker` | `luna_max` |
| Human-facing label | `luna_worker` / Luna worker | Luna Max |
| Agent-template filename | `luna-worker.toml` | `luna-max.toml` |
| Lifecycle state filename | `.ai-workflow-luna-worker.state` | `.ai-workflow-luna-max.state` |
| Lifecycle backup filename | `.ai-workflow-luna-worker.backup` | `.ai-workflow-luna-max.backup` |

The role name in task/result artifacts remains `luna`; it denotes the workflow
role and is not a custom-agent identifier.

## Migration architecture

The root project template and Plugin release template are byte-identical and
both declare `name = "luna_max"`.  Runtime identity contracts expect
`agent_type = "luna_max"` only on the `NATIVE_SUBAGENT` surface.  The
`CODEX_EXEC_ROLE_CONTRACT` surface continues to use no custom agent type.

Installer lifecycle operations publish the canonical `luna-max.toml` and
canonical state files.  A pre-existing, verified legacy `luna-worker.toml`
installation may be migrated atomically to the canonical filenames.  An
unknown, modified, unsafe, unreadable, or simultaneously present legacy and
canonical installation fails closed; the installer must not overwrite it.
Uninstall removes only files owned by the canonical or verified migrated
installation, preserving user-owned content.

New route, launch, runtime-evidence, cost, and result processing accepts only
`luna_max`; an old native `luna_worker` observation is rejected with the
existing runtime identity conflict behavior.  Legacy is therefore migration
input for the installer, not a selectable execution identity.

## Distribution and documentation

README, orchestration skill, Agent metadata, Plugin verifier, release
templates, and distribution tests use **Luna Max** for reader-facing text and
`luna_max` for command/configuration identity.  The Plugin verifier confirms
root/Plugin byte parity for the renamed template and all runtime/config/schema
copies.  It also rejects an old template name in a release target unless it is
being handled through the lifecycle migration path.

## Error handling and safety

- Ambiguous old-and-new files, invalid legacy content, unsafe paths, stale
  state, failed publish, and rollback races remain fail-closed.
- A failed migration preserves the pre-existing legacy payload and does not
  leave a partial canonical publication.
- No automatic deletion of an old user-owned template occurs.
- Existing event/receipt histories are not rewritten; new observations use the
  canonical custom-agent identifier.

## Verification

Tests must prove all of the following with real lifecycle directories and
runtime observations:

1. root and Plugin `luna-max.toml` templates are byte-identical and declare
   `luna_max` / `gpt-5.6-luna` / `max`;
2. a clean install/check/uninstall uses only canonical filenames;
3. a verified old installation migrates atomically, while unknown or
   conflicting old/new files fail closed and preserve user content;
4. a native `luna_max` observation validates, and `luna_worker` is rejected;
5. `CODEX_EXEC_ROLE_CONTRACT` still rejects any custom-agent type;
6. distribution verification fails if a renamed mirror or template is
   tampered with;
7. all published text avoids treating `luna_worker` as a current selectable
   agent.

## Non-goals

- Changing the `luna` workflow-role field;
- changing models, reasoning effort, permissions, or construction-envelope
  requirements;
- rewriting historical ledgers or runtime receipts;
- automatic merge, push, or removal of a user-owned installation.
