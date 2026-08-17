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
import secrets
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


Tombstone = tuple[str, int, tuple[int, int, str]]


def _new_tombstone(directory: int) -> tuple[str, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    for _ in range(32):
        name = f".ai-workflow-cleanup-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            continue
        return name, os.open(
            name, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory
        )
    _fail()
    raise AssertionError("unreachable")


def _remove_tombstone_directory(directory: int, name: str, descriptor: int) -> None:
    os.close(descriptor)
    os.rmdir(name, dir_fd=directory)


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _restore_tombstone(directory: int, original_name: str, tombstone: Tombstone) -> bool:
    _, descriptor, expected = tombstone
    try:
        os.link(
            "payload",
            original_name,
            src_dir_fd=descriptor,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    except OSError:
        return False
    if _identity(directory, original_name) != expected:
        return False
    return _quarantine_tombstone(tombstone)


def _quarantine_tombstone(tombstone: Tombstone) -> bool:
    """Move payload once, verify it, and retain it for deferred cleanup.

    POSIX has no portable unlink-by-inode operation.  Performing another
    check-then-unlink would reintroduce the replacement race this helper is
    designed to close, so neither the retained entry nor its private directory
    is destroyed here.
    """

    _, descriptor, expected = tombstone
    retained_name = f"retained-{secrets.token_hex(16)}"
    try:
        os.rename("payload", retained_name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        matched = _identity(descriptor, retained_name) == expected
    except OSError:
        matched = False
    _close_quietly(descriptor)
    return matched


def _retire_verified(
    directory: int, name: str, expected: tuple[int, int, str]
) -> Tombstone:
    tombstone_name, descriptor = _new_tombstone(directory)
    tombstone = (tombstone_name, descriptor, expected)
    try:
        os.rename(name, "payload", src_dir_fd=directory, dst_dir_fd=descriptor)
    except BaseException:
        try:
            _remove_tombstone_directory(directory, tombstone_name, descriptor)
        except OSError:
            pass
        raise
    if _identity(descriptor, "payload") != expected:
        if not _restore_tombstone(directory, name, tombstone):
            _close_quietly(descriptor)
        _fail()
    return tombstone


def _discard_tombstone(directory: int, tombstone: Tombstone) -> bool:
    del directory
    return _quarantine_tombstone(tombstone)


def _rollback_tombstones(
    directory: int, retired: dict[str, Tombstone]
) -> None:
    for name, tombstone in reversed(tuple(retired.items())):
        if not _restore_tombstone(directory, name, tombstone):
            _close_quietly(tombstone[1])


def _retire_all(
    directory: int, snapshots: dict[str, tuple[int, int, str]]
) -> dict[str, Tombstone]:
    retired: dict[str, Tombstone] = {}
    try:
        for name, identity in snapshots.items():
            retired[name] = _retire_verified(directory, name, identity)
        return retired
    except BaseException:
        _rollback_tombstones(directory, retired)
        raise


def _discard_all(directory: int, retired: dict[str, Tombstone]) -> bool:
    discarded = True
    for item in retired.values():
        if not _discard_tombstone(directory, item):
            discarded = False
    return discarded


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
        retired = {target_name: _retire_verified(directory, target_name, target)}
        return 0 if _discard_all(directory, retired) else 1
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
    retired = _retire_all(directory, snapshots)
    if backup_sha is not None:
        backup_tombstone = retired[backup_name]
        try:
            os.link(
                "payload",
                target_name,
                src_dir_fd=backup_tombstone[1],
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except OSError:
            _rollback_tombstones(directory, retired)
            return 1
        if _identity(directory, target_name) != backup_tombstone[2]:
            _rollback_tombstones(directory, retired)
            return 1
    return 0 if _discard_all(directory, retired) else 1


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
