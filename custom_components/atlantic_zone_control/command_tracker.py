"""Tracking of in-flight Overkiz executions.

Maps an ``exec_id`` back to the devices whose commands it carries, so a failed
execution invalidates exactly the optimistic state it should and no more. Every
entry carries an age: an execution that never reaches a terminal state must
still expire, because anything still tracked pins the fast poll.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import time
from typing import Any


@dataclass(slots=True)
class PendingExecution:
    """An execution the coordinator is waiting on."""

    exec_id: str
    device_urls: frozenset[str] = field(default_factory=frozenset)
    command_names: tuple[str, ...] = ()
    # Resolved at call time, not bound at class definition: a directly-bound
    # time.monotonic escapes test clock patching and ages every entry instantly.
    updated_at: float = field(default_factory=lambda: time.monotonic())

    @property
    def age(self) -> float:
        """Seconds since this execution was registered or last progressed."""
        return time.monotonic() - self.updated_at

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view, for diagnostics."""
        return {
            "exec_id": self.exec_id,
            "device_urls": sorted(self.device_urls),
            "command_names": list(self.command_names),
            "age": round(self.age, 1),
        }


class CommandTracker:
    """Track in-flight executions and the devices they affect."""

    def __init__(self) -> None:
        """Initialize an empty tracker."""
        self._pending: dict[str, PendingExecution] = {}

    def __bool__(self) -> bool:
        """Return True while any execution is in flight."""
        return bool(self._pending)

    def __len__(self) -> int:
        """Return the number of tracked executions."""
        return len(self._pending)

    def register(
        self,
        exec_id: str,
        device_urls: Iterable[str],
        command_names: Iterable[str],
    ) -> None:
        """Track an execution this integration started."""
        urls = frozenset(device_urls)
        names = tuple(command_names)

        if (existing := self._pending.get(exec_id)) is not None:
            # The server may merge concurrent action groups under one exec_id.
            urls |= existing.device_urls
            names = existing.command_names + names

        self._pending[exec_id] = PendingExecution(
            exec_id=exec_id, device_urls=urls, command_names=names
        )

    def register_foreign(self, exec_id: str) -> None:
        """Track an execution started elsewhere (Somfy app, wall thermostat).

        Deliberately recorded with no device URLs: a foreign execution failing
        must never roll back an unrelated optimistic value of ours. Its only
        purpose is to expire, so it cannot pin the fast poll forever.
        """
        if exec_id not in self._pending:
            self._pending[exec_id] = PendingExecution(exec_id=exec_id)

    def touch(self, exec_id: str) -> None:
        """Reset the age of an execution that reported progress."""
        if (pending := self._pending.get(exec_id)) is not None:
            pending.updated_at = time.monotonic()

    def pop(self, exec_id: str) -> PendingExecution | None:
        """Remove and return a tracked execution, if known."""
        return self._pending.pop(exec_id, None)

    def sweep(self, max_age: float) -> list[PendingExecution]:
        """Remove and return executions that never reached a terminal state."""
        stale = [p for p in self._pending.values() if p.age > max_age]
        for pending in stale:
            del self._pending[pending.exec_id]
        return stale

    def has_own_commands(self) -> bool:
        """Return True while a command this integration sent is in flight.

        Refresh executions and executions started elsewhere carry no device
        URLs, so they do not count: they change no state and must not block
        reconciliation.
        """
        return any(p.device_urls for p in self._pending.values())

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-serialisable view of everything in flight."""
        return [p.as_dict() for p in self._pending.values()]
