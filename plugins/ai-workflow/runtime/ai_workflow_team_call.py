"""Pure parsing, classification, and append-only receipts for Team Call."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Literal, Mapping


DIRECTIVE_VERSION = "team-call-1"


class TeamCallError(ValueError):
    """A stable, fail-closed Team Call contract error."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class TeamCall:
    raw_message: str
    objective: str
    raw_request_sha256: str


@dataclass(frozen=True)
class TeamCallIntent:
    disposition: Literal["DIRECT_L0", "DIRECT_L1", "PLAN_REQUIRED", "BLOCKED"]
    risk_reasons: tuple[str, ...]
    l0_action: str | None
    evidence_path: str | None


@dataclass(frozen=True)
class TeamCallReceipt:
    call_id: str
    raw_request_sha256: str
    intake_sha256: str
    disposition: str
    risk_reasons: tuple[str, ...]
    task_id: str | None
    created_at_utc: str
    result_sha256: str | None


@dataclass(frozen=True)
class TeamCallRoute:
    task_id: str | None
    result_sha256: str | None


_DIRECTIVE = re.compile(
    r"^[ \t]*team[ \t]+call(?P<separator>[ \t]+|:|：)(?P<objective>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_NAME = re.compile(r"^[ \t]*team[ \t]+call", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_L1 = re.compile(r"^核对文件 +([^ ]+)$")
_UNSAFE_METACHARACTERS = frozenset(";|&$`\n\r\\")

# The controller owns execution.  This pure module only binds an allowlisted
# name to its reviewable, fixed argv for callers and tests.
L0_FIXED_ARGV: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "workspace_status": ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        "plugin_mirror_verify": ("sh", "plugins/ai-workflow/scripts/verify.sh"),
        "workflow_verify": ("sh", "scripts/verify_all.sh"),
    }
)
_L0_OBJECTIVES: Mapping[str, str] = MappingProxyType(
    {
        "检查当前工作区状态": "workspace_status",
        "核对 plugin 根/镜像一致性": "plugin_mirror_verify",
        "运行完整验证": "workflow_verify",
    }
)

_EVENT_FIELDS = frozenset(
    {
        "event",
        "directive_version",
        "call_id",
        "raw_request_sha256",
        "intake_sha256",
        "objective",
        "disposition",
        "risk_reasons",
        "l0_action",
        "evidence_path",
        "task_id",
        "created_at_utc",
        "result_sha256",
        "route_status",
    }
)


class _DuplicateJsonMember(ValueError):
    """A JSON object with duplicate names is not an exact ledger event."""


def _json_object_without_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, member in pairs:
        if name in value:
            raise _DuplicateJsonMember(name)
        value[name] = member
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TeamCallError("TEAM_CALL_INVALID", "record is not JSON serializable") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_objective(value: str) -> str:
    return " ".join(value.split())


def parse_team_call(message: str) -> TeamCall | None:
    """Parse only a leading Team Call directive without accepting prose mentions."""

    if not isinstance(message, str):
        raise TeamCallError("TEAM_CALL_INVALID", "message must be a string")
    matched = _DIRECTIVE.match(message)
    if matched is None:
        leading = _LEADING_NAME.match(message)
        if leading is None:
            return None
        if message[leading.end() :] == "":
            raise TeamCallError("TEAM_CALL_EMPTY", "team call requires an objective")
        raise TeamCallError("TEAM_CALL_INVALID", "team call separator is invalid")
    objective = _normalize_objective(matched.group("objective"))
    if not objective:
        raise TeamCallError("TEAM_CALL_EMPTY", "team call requires an objective")
    return TeamCall(message, objective, _sha256(message))


def _intake_value(call: TeamCall, intent: TeamCallIntent) -> dict[str, object]:
    return {
        "directive_version": DIRECTIVE_VERSION,
        "objective": _normalize_objective(call.objective),
        "disposition": intent.disposition,
        "risk_reasons": list(intent.risk_reasons),
        "l0_action": intent.l0_action,
        "evidence_path": intent.evidence_path,
    }


def _intake_sha256(call: TeamCall, intent: TeamCallIntent) -> str:
    return _sha256(_canonical_json(_intake_value(call, intent)))


def _identity_value(call: TeamCall, intent: TeamCallIntent) -> dict[str, object]:
    return {
        "directive_version": DIRECTIVE_VERSION,
        "raw_request_sha256": call.raw_request_sha256,
        "intake": _intake_value(call, intent),
    }


def team_call_id(call: TeamCall, intent: TeamCallIntent) -> str:
    """Return the stable ID that binds raw request evidence to normalized intake."""

    return _sha256(_canonical_json(_identity_value(call, intent)))


def _evidence_path_is_safe(path: str) -> bool:
    if not path or "\x00" in path or path.startswith("~"):
        return False
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def classify_team_call(call: TeamCall) -> TeamCallIntent:
    """Conservatively classify a parsed directive; never interpret user commands."""

    if not isinstance(call, TeamCall):
        raise TeamCallError("TEAM_CALL_INVALID", "call must be a TeamCall")
    if not _normalize_objective(call.objective):
        raise TeamCallError("TEAM_CALL_EMPTY", "team call requires an objective")
    if any(character in call.raw_message or character in call.objective for character in _UNSAFE_METACHARACTERS):
        raise TeamCallError("TEAM_CALL_UNSAFE_INPUT", "shell metacharacters are forbidden")

    objective = _normalize_objective(call.objective)
    action = _L0_OBJECTIVES.get(objective.casefold())
    if action is not None:
        return TeamCallIntent("DIRECT_L0", ("FIXED_L0_ALLOWLIST",), action, None)

    l1 = _L1.fullmatch(objective)
    if l1 is not None:
        evidence_path = l1.group(1)
        if not _evidence_path_is_safe(evidence_path):
            raise TeamCallError("TEAM_CALL_EVIDENCE_INVALID", "evidence path must be repo-relative")
        return TeamCallIntent("DIRECT_L1", ("READ_ONLY_FILE_EVIDENCE",), None, evidence_path)

    if objective.startswith("核对文件"):
        # This is recognizably a file-evidence request, but its scope is not
        # precise enough to direct-execute.
        suffix = objective.removeprefix("核对文件").strip()
        if suffix and ("\x00" in suffix or suffix.startswith("/") or ".." in suffix.split("/")):
            raise TeamCallError("TEAM_CALL_EVIDENCE_INVALID", "evidence path must be repo-relative")
    return TeamCallIntent("PLAN_REQUIRED", ("PLAN_REQUIRED",), None, None)


class TeamCallRegistry:
    """A single-lock, append-only receipt journal for parsed Team Calls."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.ledger_path = self.root / "team-calls.jsonl"
        self.lock_path = self.root / ".team-call.lock"

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TeamCallError("TEAM_CALL_ALREADY_RUNNING", "another team call holds the registry lock") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _new_receipt(self, call: TeamCall, intent: TeamCallIntent) -> TeamCallReceipt:
        return TeamCallReceipt(
            call_id=team_call_id(call, intent),
            raw_request_sha256=call.raw_request_sha256,
            intake_sha256=_intake_sha256(call, intent),
            disposition=intent.disposition,
            risk_reasons=intent.risk_reasons,
            task_id=None,
            created_at_utc=self._now(),
            result_sha256=None,
        )

    @staticmethod
    def _intent_from_event(row: Mapping[str, object]) -> TeamCallIntent:
        return TeamCallIntent(
            row["disposition"],  # type: ignore[arg-type]
            tuple(row["risk_reasons"]),  # type: ignore[arg-type]
            row["l0_action"],  # type: ignore[arg-type]
            row["evidence_path"],  # type: ignore[arg-type]
        )

    @staticmethod
    def _call_from_event(row: Mapping[str, object]) -> TeamCall:
        return TeamCall("", row["objective"], row["raw_request_sha256"])  # type: ignore[arg-type]

    def _event(self, event: str, receipt: TeamCallReceipt, call: TeamCall, intent: TeamCallIntent, *, route_status: str) -> dict[str, object]:
        return {
            "event": event,
            "directive_version": DIRECTIVE_VERSION,
            "call_id": receipt.call_id,
            "raw_request_sha256": receipt.raw_request_sha256,
            "intake_sha256": receipt.intake_sha256,
            "objective": _normalize_objective(call.objective),
            "disposition": receipt.disposition,
            "risk_reasons": list(receipt.risk_reasons),
            "l0_action": intent.l0_action,
            "evidence_path": intent.evidence_path,
            "task_id": receipt.task_id,
            "created_at_utc": receipt.created_at_utc,
            "result_sha256": receipt.result_sha256,
            "route_status": route_status,
        }

    def _received_event(self, receipt: TeamCallReceipt, call: TeamCall, intent: TeamCallIntent) -> dict[str, object]:
        return self._event("TEAM_CALL_RECEIVED", receipt, call, intent, route_status="RECEIVED")

    def _append_event(self, event: Mapping[str, object]) -> None:
        line = _canonical_json(dict(event)) + "\n"
        try:
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "cannot append team call ledger") from exc

    def _append_received(self, receipt: TeamCallReceipt, call: TeamCall, intent: TeamCallIntent) -> None:
        self._append_event(self._received_event(receipt, call, intent))

    @staticmethod
    def _valid_utc_timestamp(value: object) -> bool:
        if not isinstance(value, str) or not value.endswith("Z"):
            return False
        try:
            datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_sha256_or_none(value: object) -> bool:
        return value is None or (isinstance(value, str) and _SHA256.fullmatch(value) is not None)

    def _validate_event(self, row: object) -> dict[str, object]:
        if not isinstance(row, dict) or set(row) != _EVENT_FIELDS:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger event shape is invalid")
        if row["event"] not in {"TEAM_CALL_RECEIVED", "TEAM_CALL_ROUTED"}:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger event type is invalid")
        if row["directive_version"] != DIRECTIVE_VERSION:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger directive version is invalid")
        for name in ("call_id", "raw_request_sha256", "intake_sha256"):
            if not isinstance(row[name], str) or _SHA256.fullmatch(row[name]) is None:
                raise TeamCallError("TEAM_CALL_LEDGER_INVALID", f"ledger {name} is invalid")
        if not isinstance(row["objective"], str) or not row["objective"]:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger objective is invalid")
        if row["disposition"] not in {"DIRECT_L0", "DIRECT_L1", "PLAN_REQUIRED", "BLOCKED"}:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger disposition is invalid")
        if not isinstance(row["risk_reasons"], list) or not all(isinstance(reason, str) for reason in row["risk_reasons"]):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger risk reasons are invalid")
        if row["l0_action"] is not None and not isinstance(row["l0_action"], str):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger action is invalid")
        if row["evidence_path"] is not None and not isinstance(row["evidence_path"], str):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger evidence path is invalid")
        if row["task_id"] is not None and (not isinstance(row["task_id"], str) or not row["task_id"]):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger task id is invalid")
        if not self._valid_utc_timestamp(row["created_at_utc"]) or not self._is_sha256_or_none(row["result_sha256"]):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger terminal fields are invalid")
        if row["event"] == "TEAM_CALL_RECEIVED":
            if row["route_status"] != "RECEIVED" or row["task_id"] is not None or row["result_sha256"] is not None:
                raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "received event must be non-terminal")
        elif row["route_status"] not in {"ROUTED", "BLOCKED"}:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "routed event status is invalid")
        if row["route_status"] == "BLOCKED" and (row["task_id"] is not None or row["result_sha256"] is not None):
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "blocked route must not bind a result")

        event_call = self._call_from_event(row)
        event_intent = self._intent_from_event(row)
        try:
            expected_intent = classify_team_call(event_call)
        except TeamCallError as exc:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger intake is not classifiable") from exc
        if event_intent != expected_intent:
            raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "ledger intake disagrees with classifier")
        if _intake_sha256(event_call, event_intent) != row["intake_sha256"]:
            raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "ledger intake digest disagrees with intake")
        if team_call_id(event_call, event_intent) != row["call_id"]:
            raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "ledger call id disagrees with identity")
        return row

    @staticmethod
    def _received_and_routed_identity_match(received: Mapping[str, object], routed: Mapping[str, object]) -> bool:
        return all(
            received[field] == routed[field]
            for field in (
                "directive_version",
                "call_id",
                "raw_request_sha256",
                "intake_sha256",
                "objective",
                "disposition",
                "risk_reasons",
                "l0_action",
                "evidence_path",
                "created_at_utc",
            )
        )

    def _load_history(self) -> tuple[dict[str, list[dict[str, object]]], dict[str, object] | None]:
        if not self.ledger_path.exists():
            return {}, None
        histories: dict[str, list[dict[str, object]]] = {}
        pending: dict[str, object] | None = None
        try:
            with self.ledger_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.endswith("\n") or not line.strip():
                        raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger must contain complete JSONL rows")
                    try:
                        parsed = json.loads(line, object_pairs_hook=_json_object_without_duplicate_members)
                    except (json.JSONDecodeError, _DuplicateJsonMember) as exc:
                        raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger JSON is invalid") from exc
                    event = self._validate_event(parsed)
                    call_id = event["call_id"]
                    if event["event"] == "TEAM_CALL_RECEIVED":
                        if pending is not None or call_id in histories:
                            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger received event is not globally serial")
                        histories[call_id] = [event]  # type: ignore[index]
                        pending = event
                        continue
                    if pending is None or pending["call_id"] != call_id:
                        raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger routed event has no matching receipt")
                    if not self._received_and_routed_identity_match(pending, event):
                        raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "received and routed identities differ")
                    histories[call_id].append(event)  # type: ignore[index]
                    pending = None
        except UnicodeError as exc:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "ledger is not valid UTF-8") from exc
        except OSError as exc:
            raise TeamCallError("TEAM_CALL_LEDGER_INVALID", "cannot read team call ledger") from exc
        return histories, pending

    def _receipt_from_routed_event(self, row: Mapping[str, object]) -> TeamCallReceipt:
        disposition = "BLOCKED" if row["route_status"] == "BLOCKED" else row["disposition"]
        return TeamCallReceipt(
            call_id=row["call_id"],  # type: ignore[arg-type]
            raw_request_sha256=row["raw_request_sha256"],  # type: ignore[arg-type]
            intake_sha256=row["intake_sha256"],  # type: ignore[arg-type]
            disposition=disposition,  # type: ignore[arg-type]
            risk_reasons=tuple(row["risk_reasons"]),  # type: ignore[arg-type]
            task_id=row["task_id"],  # type: ignore[arg-type]
            created_at_utc=row["created_at_utc"],  # type: ignore[arg-type]
            result_sha256=row["result_sha256"],  # type: ignore[arg-type]
        )

    @staticmethod
    def _validate_route(route: object) -> TeamCallRoute:
        if not isinstance(route, TeamCallRoute):
            raise TeamCallError("TEAM_CALL_INVALID", "executor must return a TeamCallRoute")
        if route.task_id is not None and (not isinstance(route.task_id, str) or not route.task_id):
            raise TeamCallError("TEAM_CALL_INVALID", "route task id is invalid")
        if route.result_sha256 is not None and (
            not isinstance(route.result_sha256, str) or _SHA256.fullmatch(route.result_sha256) is None
        ):
            raise TeamCallError("TEAM_CALL_INVALID", "route result digest is invalid")
        return route

    def execute_once(
        self,
        call: TeamCall,
        intent: TeamCallIntent,
        executor: Callable[[TeamCallReceipt], TeamCallRoute],
    ) -> TeamCallReceipt:
        """Append one received/terminal pair or return the matching terminal receipt."""

        if not callable(executor):
            raise TeamCallError("TEAM_CALL_INVALID", "executor must be callable")
        if not isinstance(call, TeamCall):
            raise TeamCallError("TEAM_CALL_INVALID", "call must be a TeamCall")
        parsed_call = parse_team_call(call.raw_message)
        if parsed_call is None or parsed_call != call:
            raise TeamCallError("TEAM_CALL_INVALID", "call must be the exact parsed directive")
        if classify_team_call(call) != intent:
            raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "intent disagrees with classifier")
        receipt = self._new_receipt(call, intent)
        with self._locked():
            histories, pending = self._load_history()
            if pending is not None:
                raise TeamCallError("TEAM_CALL_ALREADY_RUNNING", "team call has a non-terminal receipt")
            existing = histories.get(receipt.call_id)
            if existing is not None:
                first = existing[0]
                if (
                    first["raw_request_sha256"] != receipt.raw_request_sha256
                    or first["intake_sha256"] != receipt.intake_sha256
                ):
                    raise TeamCallError("TEAM_CALL_IDENTITY_DRIFT", "same call id has different identity")
                if len(existing) == 1:
                    raise TeamCallError("TEAM_CALL_ALREADY_RUNNING", "team call has a non-terminal receipt")
                return self._receipt_from_routed_event(existing[1])

            self._append_received(receipt, call, intent)
            try:
                route = self._validate_route(executor(receipt))
            except BaseException:
                self._append_event(
                    self._event("TEAM_CALL_ROUTED", receipt, call, intent, route_status="BLOCKED")
                )
                raise
            routed = replace(receipt, task_id=route.task_id, result_sha256=route.result_sha256)
            self._append_event(self._event("TEAM_CALL_ROUTED", routed, call, intent, route_status="ROUTED"))
            return routed
