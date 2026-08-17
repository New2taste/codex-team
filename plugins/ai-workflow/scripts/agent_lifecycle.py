#!/usr/bin/env python3
"""Cleanup-only support for historical Luna Agent installs.

This release never creates or publishes an Agent template. Both compatibility
entrypoints call the same conservative cleanup operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat


TARGET_FILENAME = "luna-max.toml"
STATE_FILENAME = ".ai-workflow-luna-max.state"
BACKUP_FILENAME = ".ai-workflow-luna-max.backup"
LEGACY_TARGET_FILENAME = "luna-worker.toml"
LEGACY_STATE_FILENAME = ".ai-workflow-luna-worker.state"
LEGACY_BACKUP_FILENAME = ".ai-workflow-luna-worker.backup"
CANONICAL_RELEASE_SHA256 = "6237649deb278392111355490a9c71c00be66388c6fb25435694d00eb6f18bbb"
LEGACY_RELEASE_SHA256 = "60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024"


class LifecycleError(RuntimeError):
    pass


def _fail() -> None:
    raise LifecycleError("unsafe or unverified Agent cleanup input")


def _components(value: str | os.PathLike[str]) -> list[str]:
    raw = os.fspath(value)
    if not raw:
        _fail()
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    parts = [part for part in raw.split(os.sep) if part]
    if not parts or any(part in {".", ".."} for part in parts):
        _fail()
    return parts


def _open_directory(value: str | os.PathLike[str]) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in _components(value):
            try:
                next_descriptor = os.open(
                    component, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor
                )
            except FileNotFoundError:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(directory: int, name: str) -> tuple[int, int, str] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleError("unsafe or unreadable Agent cleanup input") from exc
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            _fail()
        digest = hashlib.sha256(handle.read()).hexdigest()
    return status.st_dev, status.st_ino, digest


def _read(directory: int, name: str) -> tuple[bytes, tuple[int, int, str]]:
    before = _identity(directory, name)
    if before is None:
        _fail()
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "rb") as handle:
        content = handle.read()
        status = os.fstat(handle.fileno())
    after = (status.st_dev, status.st_ino, hashlib.sha256(content).hexdigest())
    if after != before:
        _fail()
    return content, after


def _state(content: bytes, target_name: str, target_sha: str) -> str | None:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("invalid Agent cleanup state") from exc
    required = {
        "plugin_version", "target_filename", "installed_sha256",
        "installed_at_utc", "backup_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        _fail()
    if value["target_filename"] != target_name or value["installed_sha256"] != target_sha:
        _fail()
    backup_sha = value["backup_sha256"]
    if backup_sha is not None and (not isinstance(backup_sha, str) or len(backup_sha) != 64):
        _fail()
    return backup_sha


def _cleanup_family(
    directory: int,
    target_name: str,
    state_name: str,
    backup_name: str,
    *,
    expected_sha: str,
    unmanaged_sha: str | None = None,
) -> int:
    target = _identity(directory, target_name)
    state = _identity(directory, state_name)
    backup = _identity(directory, backup_name)
    if target is None and state is None and backup is None:
        return 0
    if target is None:
        return 1
    if state is None:
        if backup is not None or unmanaged_sha is None or target[2] != unmanaged_sha:
            return 1
        if _identity(directory, target_name) != target:
            return 1
        os.unlink(target_name, dir_fd=directory)
        return 0
    if target[2] != expected_sha:
        return 1
    state_content, state_snapshot = _read(directory, state_name)
    backup_sha = _state(state_content, target_name, target[2])
    snapshots = {target_name: target, state_name: state_snapshot}
    if backup_sha is None:
        if backup is not None:
            return 1
    else:
        if backup is None or backup[2] != backup_sha:
            return 1
        snapshots[backup_name] = backup
    if any(_identity(directory, name) != identity for name, identity in snapshots.items()):
        return 1
    if backup_sha is None:
        os.unlink(target_name, dir_fd=directory)
    else:
        os.rename(backup_name, target_name, src_dir_fd=directory, dst_dir_fd=directory)
    os.unlink(state_name, dir_fd=directory)
    return 0


def cleanup(target_directory: str | os.PathLike[str], *, check: bool = False) -> int:
    directory: int | None = None
    try:
        directory = _open_directory(target_directory)
        if directory is None:
            return 0
        canonical = any(
            _identity(directory, name) is not None
            for name in (TARGET_FILENAME, STATE_FILENAME, BACKUP_FILENAME)
        )
        legacy = any(
            _identity(directory, name) is not None
            for name in (LEGACY_TARGET_FILENAME, LEGACY_STATE_FILENAME, LEGACY_BACKUP_FILENAME)
        )
        if canonical and legacy:
            return 1
        if check:
            return 1 if canonical or legacy else 0
        if canonical:
            return _cleanup_family(
                directory,
                TARGET_FILENAME,
                STATE_FILENAME,
                BACKUP_FILENAME,
                expected_sha=CANONICAL_RELEASE_SHA256,
            )
        if legacy:
            return _cleanup_family(
                directory,
                LEGACY_TARGET_FILENAME,
                LEGACY_STATE_FILENAME,
                LEGACY_BACKUP_FILENAME,
                expected_sha=LEGACY_RELEASE_SHA256,
                unmanaged_sha=LEGACY_RELEASE_SHA256,
            )
        return 0
    except (LifecycleError, OSError):
        return 1
    finally:
        if directory is not None:
            os.close(directory)


def install(target_directory: str | os.PathLike[str], *, check: bool = False, hook=None) -> int:
    """Compatibility alias for cleanup-only historical migration."""
    return 1 if hook is not None else cleanup(target_directory, check=check)


def uninstall(target_directory: str | os.PathLike[str], *, hook=None) -> int:
    return 1 if hook is not None else cleanup(target_directory)


def _default_target() -> str:
    codex_home = os.environ.get("CODEX_HOME")
    return (
        os.path.join(codex_home, "agents")
        if codex_home
        else os.path.join(os.path.expanduser("~"), ".codex", "agents")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--target-dir")
    parser.add_argument("--check", action="store_true")
    arguments, unknown = parser.parse_known_args(argv)
    if unknown or (arguments.operation == "uninstall" and arguments.check):
        return 1
    target = _default_target() if arguments.target_dir is None else arguments.target_dir
    return cleanup(target, check=arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
