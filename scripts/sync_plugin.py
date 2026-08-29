"""Synchronize the fixed root-to-Plugin distribution manifest."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path


CONFIG_FILES = (
    "ai_workflow.toml",
    "ai_workflow_task.schema.json",
    "ai_workflow_result.schema.json",
    "ai_workflow_route_request.schema.json",
    "ai_workflow_route_decision.schema.json",
    "ai_workflow_route_declaration.schema.json",
    "ai_workflow_candidate_state.schema.json",
    "ai_workflow_final_verdict.schema.json",
    "ai_workflow_ownership_registry.schema.json",
    "ai_workflow_side_effect.schema.json",
    "ai_workflow_owner_authorization.schema.json",
    "ai_workflow_route_advice.schema.json",
    "ai_workflow_plan.schema.json",
    "ai_workflow_runtime_evidence.schema.json",
    "ai_workflow_cost_evidence.schema.json",
    "ai_workflow_router_probe_manifest.schema.json",
    "ai_workflow_scheduler.schema.json",
)
RUNTIME_FILES = (
    "ai_workflow.py",
    "ai_workflow_artifacts.py",
    "ai_workflow_routing.py",
    "ai_workflow_declarations.py",
    "ai_workflow_candidate_state.py",
    "ai_workflow_verdicts.py",
    "ai_workflow_ownership.py",
    "ai_workflow_side_effects.py",
    "ai_workflow_authorizations.py",
    "ai_workflow_planning.py",
    "ai_workflow_runtime.py",
    "ai_workflow_costs.py",
    "ai_workflow_repairs.py",
    "ai_workflow_team_call.py",
    "ai_workflow_scheduler.py",
)


class SyncError(RuntimeError):
    pass


def _manifest(root: Path) -> tuple[tuple[Path, Path], ...]:
    plugin = root / "plugins" / "ai-workflow"
    return tuple(
        (root / "config" / name, plugin / "config" / name)
        for name in CONFIG_FILES
    ) + tuple(
        (root / "scripts" / name, plugin / "runtime" / name)
        for name in RUNTIME_FILES
    )


def _replace_from_source(source: Path, target: Path) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        source_mode = stat.S_IMODE(source.stat().st_mode)
        os.chmod(temporary, source_mode)
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise SyncError(f"cannot synchronize {target}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def synchronize(root: Path, *, write: bool) -> tuple[str, ...]:
    root = Path(root).resolve()
    changed: list[str] = []
    for source, target in _manifest(root):
        if not source.is_file() or source.is_symlink():
            raise SyncError(f"source is not a regular file: {source}")
        if not target.is_file() or target.is_symlink():
            raise SyncError(f"target is not a regular file: {target}")
        if source.read_bytes() == target.read_bytes():
            continue
        relative_target = target.relative_to(root).as_posix()
        if not write:
            raise SyncError(f"Plugin copy differs: {relative_target}")
        _replace_from_source(source, target)
        changed.append(relative_target)
    return tuple(changed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sync-plugin")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        changed = synchronize(args.root, write=args.write)
    except SyncError as exc:
        print(f"PLUGIN_SYNC_FAILED: {exc}")
        return 1
    if changed:
        for path in changed:
            print(f"SYNCED {path}")
    else:
        print("PLUGIN_SYNC_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
