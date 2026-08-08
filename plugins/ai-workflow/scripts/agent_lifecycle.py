#!/usr/bin/env python3
"""Fail-closed, data-preserving lifecycle operations for the Luna template.

The shell entrypoints intentionally delegate filesystem mutation here.  The
implementation holds a directory descriptor opened component-by-component with
``O_NOFOLLOW`` so later path swaps cannot redirect operations.  New entries are
published with hard links (no clobber); replacements are first moved into an
owned tombstone directory and re-hashed before any publish or delete step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PLUGIN_VERSION = "0.2.0"
TARGET_FILENAME = "luna-worker.toml"
STATE_FILENAME = ".ai-workflow-luna-worker.state"
BACKUP_FILENAME = ".ai-workflow-luna-worker.backup"
RELEASE_SHA256 = "60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024"
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
Hook = Callable[[str], None]
FileIdentity = tuple[int, int, str]


class LifecycleError(RuntimeError):
    """A fail-closed lifecycle condition with no user path in its text."""


def _fail(message: str) -> None:
    raise LifecycleError(message)


def _raw_path_components(value: str | os.PathLike[str]) -> list[str]:
    raw = os.fspath(value)
    if not raw:
        _fail("empty target directory")
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    components: list[str] = []
    for component in raw.split(os.sep):
        if not component:
            continue
        if component in {".", ".."}:
            _fail("unsafe target directory")
        components.append(component)
    if not components:
        _fail("refusing filesystem root")
    return components


def _open_target_directory(
    value: str | os.PathLike[str], *, create: bool
) -> int | None:
    """Open the raw target path without ever resolving a symbolic-link parent."""

    components = _raw_path_components(value)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in components:
            try:
                next_descriptor = os.open(
                    component, flags | nofollow, dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    component, flags | nofollow, dir_fd=descriptor
                )
            except OSError as exc:
                _fail("unsafe target directory")
                raise AssertionError("unreachable") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_stat(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail("unsafe target entry")
        raise AssertionError("unreachable") from exc


def _require_regular(directory: int, name: str) -> os.stat_result:
    status = _entry_stat(directory, name)
    if status is None or stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        _fail("unsafe target entry")
    return status


def _read_regular(directory: int, name: str) -> bytes:
    _require_regular(directory, name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                _fail("unsafe target entry")
            return handle.read()
    except OSError as exc:
        _fail("unreadable target entry")
        raise AssertionError("unreachable") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_regular(directory: int, name: str) -> str:
    return _sha256(_read_regular(directory, name))


def _stage(directory: int, label: str, content: bytes) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = f".ai-workflow-{label}-{secrets.token_hex(16)}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(name, dir_fd=directory)
            except OSError:
                pass
            raise
        return name
    _fail("unable to stage lifecycle file")
    raise AssertionError("unreachable")


def _publish_no_clobber(
    source_directory: int, source_name: str, destination_directory: int, destination_name: str
) -> bool:
    """Publish by hard-link only, so a newly-created destination is never replaced."""

    try:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_directory,
            dst_dir_fd=destination_directory,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    except OSError as exc:
        _fail("cannot publish lifecycle file")
        raise AssertionError("unreachable") from exc
    return True


def _publish_staged_no_clobber(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
    expected_identity: FileIdentity,
) -> bool:
    """Publish only the staged inode and verify that exact inode was linked."""

    if _file_identity(source_directory, source_name) != expected_identity:
        return False
    try:
        published = _publish_no_clobber(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )
    except BaseException:
        _discard_owned_publication(
            destination_directory, destination_name, expected_identity
        )
        raise
    if not published:
        return False
    try:
        destination_matches = (
            _file_identity(destination_directory, destination_name)
            == expected_identity
        )
    except BaseException:
        _discard_owned_publication(
            destination_directory, destination_name, expected_identity
        )
        raise
    if not destination_matches:
        _discard_owned_publication(
            destination_directory, destination_name, expected_identity
        )
    return destination_matches


def _make_tombstone_directory(directory: int) -> tuple[str, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    for _ in range(32):
        name = f".ai-workflow-tombstone-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            continue
        descriptor = os.open(
            name, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory
        )
        return name, descriptor
    _fail("unable to create lifecycle tombstone")
    raise AssertionError("unreachable")


def _retire_to_tombstone(directory: int, name: str) -> tuple[str, int]:
    tombstone_name, tombstone = _make_tombstone_directory(directory)
    try:
        os.rename(name, "payload", src_dir_fd=directory, dst_dir_fd=tombstone)
    except OSError as exc:
        os.close(tombstone)
        try:
            os.rmdir(tombstone_name, dir_fd=directory)
        except OSError:
            pass
        _fail("target changed during lifecycle operation")
        raise AssertionError("unreachable") from exc
    return tombstone_name, tombstone


def _preserve_tombstone(
    target_directory: int, target_name: str, tombstone: int
) -> bool:
    """Attempt a no-clobber restoration; retain tombstone if anything races."""

    return _publish_no_clobber(tombstone, "payload", target_directory, target_name)


def _discard_verified_tombstone(
    target_directory: int,
    tombstone_name: str,
    tombstone: int,
    expected_identity: FileIdentity,
) -> bool:
    """Delete only an exact re-verified payload; retain any replacement."""

    try:
        if _file_identity(tombstone, "payload") != expected_identity:
            return False
        os.unlink("payload", dir_fd=tombstone)
        os.rmdir(tombstone_name, dir_fd=target_directory)
        return True
    except (LifecycleError, OSError):
        return False


def _close_tombstone(tombstone: int) -> None:
    try:
        os.close(tombstone)
    except OSError:
        pass


def _close_directory(directory: int) -> None:
    try:
        os.close(directory)
    except OSError:
        pass


def _validate_template() -> tuple[bytes, str]:
    template = Path(__file__).resolve().parent.parent / "agents" / TARGET_FILENAME
    try:
        status = template.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            _fail("invalid release template")
        with template.open("rb") as handle:
            content = handle.read()
    except OSError as exc:
        _fail("invalid release template")
        raise AssertionError("unreachable") from exc
    digest = _sha256(content)
    if digest != RELEASE_SHA256:
        _fail("release template digest mismatch")
    try:
        value = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _fail("invalid release template")
        raise AssertionError("unreachable") from exc
    if (
        value.get("name") != "luna_worker"
        or value.get("model") != "gpt-5.6-luna"
        or value.get("model_reasoning_effort") != "max"
        or not isinstance(value.get("developer_instructions"), str)
        or "L0/L1/L2" not in value["developer_instructions"]
    ):
        _fail("invalid release template")
    return content, digest


def _parse_state(content: bytes, expected_sha: str) -> str | None:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("invalid installed state")
        raise AssertionError("unreachable") from exc
    required = {
        "plugin_version",
        "target_filename",
        "installed_sha256",
        "installed_at_utc",
        "backup_sha256",
    }
    timestamp = value.get("installed_at_utc") if isinstance(value, dict) else None
    backup = value.get("backup_sha256") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("plugin_version") != PLUGIN_VERSION
        or value.get("target_filename") != TARGET_FILENAME
        or value.get("installed_sha256") != expected_sha
        or not isinstance(timestamp, str)
        or UTC_TIMESTAMP.fullmatch(timestamp) is None
        or (backup is not None and (not isinstance(backup, str) or not re.fullmatch(r"[0-9a-f]{64}", backup)))
    ):
        _fail("invalid installed state")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        _fail("invalid installed state")
        raise AssertionError("unreachable") from exc
    return backup


def _state_content(installed_sha: str, backup_sha: str | None) -> bytes:
    value = {
        "plugin_version": PLUGIN_VERSION,
        "target_filename": TARGET_FILENAME,
        "installed_sha256": installed_sha,
        "installed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backup_sha256": backup_sha,
    }
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def _classify(directory: int | None, template_sha: str) -> tuple[str, str | None, str | None]:
    if directory is None:
        return "missing", None, None
    state_status = _entry_stat(directory, STATE_FILENAME)
    destination_status = _entry_stat(directory, TARGET_FILENAME)
    backup_status = _entry_stat(directory, BACKUP_FILENAME)
    if state_status is not None:
        _require_regular(directory, STATE_FILENAME)
        _require_regular(directory, TARGET_FILENAME)
        state_backup = _parse_state(_read_regular(directory, STATE_FILENAME), template_sha)
        destination_sha = _hash_regular(directory, TARGET_FILENAME)
        if destination_sha != template_sha:
            _fail("installed Agent differs from owned state")
        if state_backup is None:
            if backup_status is not None:
                _fail("unexpected backup")
        else:
            _require_regular(directory, BACKUP_FILENAME)
            if _hash_regular(directory, BACKUP_FILENAME) != state_backup:
                _fail("backup differs from owned state")
        return "current", destination_sha, state_backup
    if backup_status is not None:
        _fail("unexpected backup")
    if destination_status is None:
        return "missing", None, None
    _require_regular(directory, TARGET_FILENAME)
    destination_sha = _hash_regular(directory, TARGET_FILENAME)
    if destination_sha == RELEASE_SHA256:
        return "known_legacy", destination_sha, None
    return "conflict", destination_sha, None


def _hook(hook: Hook | None, point: str) -> None:
    if hook is not None:
        hook(point)


def _file_identity(directory: int, name: str) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            status = os.fstat(handle.fileno())
            if not stat.S_ISREG(status.st_mode):
                _fail("unsafe target entry")
            digest = _sha256(handle.read())
    except OSError as exc:
        _fail("unreadable target entry")
        raise AssertionError("unreachable") from exc
    return status.st_dev, status.st_ino, digest


def _discard_owned_publication(
    directory: int, name: str, identity: FileIdentity | None
) -> bool:
    """Best-effort cleanup that never deletes a replacement or raises."""

    if identity is None:
        return True
    _, _, digest = identity
    try:
        return _retire_and_discard(
            directory,
            name,
            digest,
            expected_identity=identity,
        )
    except BaseException:
        return False


def _rollback_install_publication(
    directory: int,
    agent_identity: FileIdentity | None,
    state_identity: FileIdentity | None,
    *,
    legacy_tombstone_name: str | None = None,
    legacy_tombstone: int | None = None,
    legacy_identity: FileIdentity | None = None,
) -> None:
    """Best-effort transaction rollback; original failures remain authoritative."""

    _discard_owned_publication(directory, STATE_FILENAME, state_identity)
    _discard_owned_publication(directory, TARGET_FILENAME, agent_identity)
    if (
        legacy_tombstone_name is None
        or legacy_tombstone is None
        or legacy_identity is None
    ):
        return
    try:
        restored = _preserve_tombstone(
            directory, TARGET_FILENAME, legacy_tombstone
        )
        if restored:
            _discard_verified_tombstone(
                directory,
                legacy_tombstone_name,
                legacy_tombstone,
                legacy_identity,
            )
    except BaseException:
        pass


def install(
    target_directory: str | os.PathLike[str], *, check: bool = False, hook: Hook | None = None
) -> int:
    """Install the template without overwriting any path created after preflight."""

    directory: int | None = None
    staged_agent: str | None = None
    staged_state: str | None = None
    tombstone: int | None = None
    published_agent: FileIdentity | None = None
    published_state: FileIdentity | None = None
    staged_agent_identity: FileIdentity | None = None
    staged_state_identity: FileIdentity | None = None
    try:
        template, template_sha = _validate_template()
        directory = _open_target_directory(target_directory, create=False)
        classification, expected_sha, _ = _classify(directory, template_sha)
        if check:
            return 0 if classification == "current" else 1
        if classification == "current" or classification == "conflict":
            return 0 if classification == "current" else 1
        if directory is None:
            directory = _open_target_directory(target_directory, create=True)
            if directory is None:
                _fail("cannot create target directory")
            classification, expected_sha, _ = _classify(directory, template_sha)
            if classification != "missing":
                return 1
        staged_agent = _stage(directory, "agent", template)
        staged_agent_identity = _file_identity(directory, staged_agent)
        if staged_agent_identity[2] != template_sha:
            _fail("staged template digest mismatch")
        staged_state = _stage(directory, "state", _state_content(template_sha, None))
        staged_state_identity = _file_identity(directory, staged_state)

        if classification == "missing":
            _hook(hook, "install.before_publish_missing")
            if not _publish_staged_no_clobber(
                directory,
                staged_agent,
                directory,
                TARGET_FILENAME,
                staged_agent_identity,
            ):
                return 1
            published_agent = staged_agent_identity
            try:
                _hook(hook, "install.before_publish_missing_state")
                if not _publish_staged_no_clobber(
                    directory,
                    staged_state,
                    directory,
                    STATE_FILENAME,
                    staged_state_identity,
                ):
                    _rollback_install_publication(
                        directory, published_agent, published_state
                    )
                    return 1
                published_state = staged_state_identity
                _hook(hook, "install.after_publish_missing_state")
            except BaseException:
                _rollback_install_publication(
                    directory,
                    published_agent,
                    published_state,
                )
                raise
            return 0

        if classification != "known_legacy" or expected_sha is None:
            return 1
        legacy_identity = _file_identity(directory, TARGET_FILENAME)
        if legacy_identity[2] != expected_sha:
            return 1
        _hook(hook, "install.before_retire_known_legacy")
        tombstone_name, tombstone = _retire_to_tombstone(directory, TARGET_FILENAME)
        moved_identity = _file_identity(tombstone, "payload")
        if moved_identity != legacy_identity:
            _preserve_tombstone(directory, TARGET_FILENAME, tombstone)
            return 1
        try:
            if not _publish_staged_no_clobber(
                directory,
                staged_agent,
                directory,
                TARGET_FILENAME,
                staged_agent_identity,
            ):
                _rollback_install_publication(
                    directory,
                    published_agent,
                    published_state,
                    legacy_tombstone_name=tombstone_name,
                    legacy_tombstone=tombstone,
                    legacy_identity=moved_identity,
                )
                return 1
        except BaseException:
            _rollback_install_publication(
                directory,
                published_agent,
                published_state,
                legacy_tombstone_name=tombstone_name,
                legacy_tombstone=tombstone,
                legacy_identity=moved_identity,
            )
            raise
        published_agent = staged_agent_identity
        try:
            _hook(hook, "install.before_publish_known_legacy_state")
            if not _publish_staged_no_clobber(
                directory,
                staged_state,
                directory,
                STATE_FILENAME,
                staged_state_identity,
            ):
                _rollback_install_publication(
                    directory,
                    published_agent,
                    published_state,
                    legacy_tombstone_name=tombstone_name,
                    legacy_tombstone=tombstone,
                    legacy_identity=moved_identity,
                )
                return 1
            published_state = staged_state_identity
            _hook(hook, "install.after_publish_known_legacy_state")
        except BaseException:
            _rollback_install_publication(
                directory,
                published_agent,
                published_state,
                legacy_tombstone_name=tombstone_name,
                legacy_tombstone=tombstone,
                legacy_identity=moved_identity,
            )
            raise
        discarded = _discard_verified_tombstone(
            directory, tombstone_name, tombstone, moved_identity
        )
        _close_tombstone(tombstone)
        tombstone = None
        if not discarded:
            return 1
        return 0
    except (LifecycleError, OSError):
        return 1
    finally:
        if directory is not None:
            if staged_agent is not None and staged_agent_identity is not None:
                _discard_owned_publication(
                    directory, staged_agent, staged_agent_identity
                )
            if staged_state is not None and staged_state_identity is not None:
                _discard_owned_publication(
                    directory, staged_state, staged_state_identity
                )
        if tombstone is not None:
            _close_tombstone(tombstone)
        if directory is not None:
            _close_directory(directory)


def _retire_and_discard(
    directory: int,
    name: str,
    expected_sha: str,
    *,
    expected_identity: FileIdentity | None = None,
) -> bool:
    tombstone_name, tombstone = _retire_to_tombstone(directory, name)
    try:
        moved_identity = _file_identity(tombstone, "payload")
        if expected_identity is not None and moved_identity != expected_identity:
            _preserve_tombstone(directory, name, tombstone)
            return False
        if moved_identity[2] != expected_sha:
            _preserve_tombstone(directory, name, tombstone)
            return False
        return _discard_verified_tombstone(
            directory, tombstone_name, tombstone, moved_identity
        )
    finally:
        _close_tombstone(tombstone)


def uninstall(
    target_directory: str | os.PathLike[str], *, hook: Hook | None = None
) -> int:
    """Remove only an owned, unchanged target; never delete a raced replacement."""

    directory: int | None = None
    tombstone: int | None = None
    try:
        directory = _open_target_directory(target_directory, create=False)
        if directory is None:
            return 1
        _require_regular(directory, TARGET_FILENAME)
        _require_regular(directory, STATE_FILENAME)
        state_content = _read_regular(directory, STATE_FILENAME)
        state_sha = _sha256(state_content)
        backup_sha = _parse_state(state_content, RELEASE_SHA256)
        target_identity = _file_identity(directory, TARGET_FILENAME)
        if target_identity[2] != RELEASE_SHA256:
            return 1
        state_identity = _file_identity(directory, STATE_FILENAME)
        if state_identity[2] != state_sha:
            return 1
        backup_identity: FileIdentity | None = None
        if backup_sha is not None:
            backup_identity = _file_identity(directory, BACKUP_FILENAME)
            if backup_identity[2] != backup_sha:
                return 1
        elif _entry_stat(directory, BACKUP_FILENAME) is not None:
            return 1

        _hook(hook, "uninstall.before_retire_current")
        tombstone_name, tombstone = _retire_to_tombstone(directory, TARGET_FILENAME)
        moved_identity = _file_identity(tombstone, "payload")
        if moved_identity != target_identity:
            _preserve_tombstone(directory, TARGET_FILENAME, tombstone)
            return 1

        if backup_sha is not None:
            if not _publish_no_clobber(directory, BACKUP_FILENAME, directory, TARGET_FILENAME):
                return 1
            if _hash_regular(directory, TARGET_FILENAME) != backup_sha:
                return 1
            if not _retire_and_discard(
                directory,
                BACKUP_FILENAME,
                backup_sha,
                expected_identity=backup_identity,
            ):
                return 1
        if not _retire_and_discard(
            directory,
            STATE_FILENAME,
            state_sha,
            expected_identity=state_identity,
        ):
            _preserve_tombstone(directory, TARGET_FILENAME, tombstone)
            return 1
        discarded = _discard_verified_tombstone(
            directory, tombstone_name, tombstone, moved_identity
        )
        _close_tombstone(tombstone)
        tombstone = None
        if not discarded:
            return 1
        return 0
    except (LifecycleError, OSError):
        return 1
    finally:
        if tombstone is not None:
            _close_tombstone(tombstone)
        if directory is not None:
            os.close(directory)


def _default_target() -> str:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return os.path.join(codex_home, "agents")
    return os.path.join(os.path.expanduser("~"), ".codex", "agents")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("install", "uninstall"))
    parser.add_argument("--target-dir")
    parser.add_argument("--check", action="store_true")
    arguments, unknown = parser.parse_known_args(argv)
    if unknown or (arguments.operation == "uninstall" and arguments.check):
        return 1
    target = (
        _default_target()
        if arguments.target_dir is None
        else arguments.target_dir
    )
    if arguments.operation == "install":
        return install(target, check=arguments.check)
    return uninstall(target)


if __name__ == "__main__":
    raise SystemExit(main())
