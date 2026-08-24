"""Optimistic state with a bounded lifetime.

Optimistic values keep the UI from flickering during the command debounce and
execution window. The hazard is that an optimistic value which is never
confirmed becomes a permanent lie. Every value here is therefore held until
exactly one of four things happens, and never longer:

1. Real device state confirms it.
2. The send itself raised (the entity rolls back and re-raises).
3. The owning execution reported a failure, or was swept as stale, and the
   coordinator bumped this device's invalidation generation.
4. ``OPTIMISTIC_MAX_AGE`` elapsed.

Expiry is evaluated on *read* as well as on coordinator update, so a value
cannot outlive its deadline even if the coordinator never fires again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import time
from typing import Any

from pyoverkiz.models import Command

from homeassistant.exceptions import HomeAssistantError

from .const import LOGGER, OPTIMISTIC_MAX_AGE


@dataclass(slots=True)
class OptimisticValue:
    """A value assumed to be true until confirmed, refuted, or expired."""

    value: Any
    generation: int
    set_at: float
    expiry_logged: bool = False

    @property
    def age(self) -> float:
        """Seconds since this value was set."""
        return time.monotonic() - self.set_at

    def is_expired(self, max_age: float = OPTIMISTIC_MAX_AGE) -> bool:
        """Return True once this value has outlived its deadline."""
        return self.age > max_age


class OptimisticStateMixin:
    """Optimistic value tracking shared by the climate entities.

    Expects the host to provide ``device_url`` and ``coordinator`` (an
    ``OverkizDataUpdateCoordinator``), as ``OverkizEntity`` does.
    """

    _optimistic: dict[str, OptimisticValue]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize optimistic tracking."""
        super().__init__(*args, **kwargs)
        self._optimistic = {}

    def _set_optimistic(self, field: str, value: Any) -> None:
        """Record an assumed value for a field."""
        self._optimistic[field] = OptimisticValue(
            value=value,
            generation=self.coordinator.optimistic_generation(self.device_url),
            set_at=time.monotonic(),
        )
        LOGGER.debug(
            "Optimistic %s=%s set for %s (generation %s)",
            field,
            value,
            self.device_url,
            self._optimistic[field].generation,
        )

    def _clear_optimistic(self, field: str) -> None:
        """Forget the assumed value for a field."""
        self._optimistic.pop(field, None)

    def _get_optimistic(self, field: str) -> Any | None:
        """Return the assumed value, or None once it is no longer valid.

        Evaluated on every read so a stale value cannot survive a coordinator
        that has stopped firing.
        """
        pending = self._optimistic.get(field)
        if pending is None:
            return None

        if self._is_invalidated(pending):
            self._clear_optimistic(field)
            return None

        if pending.is_expired():
            self._log_expiry_once(field, pending)
            self._clear_optimistic(field)
            return None

        return pending.value

    def _is_invalidated(self, pending: OptimisticValue) -> bool:
        """Return True if the coordinator refuted this device's assumptions."""
        current = self.coordinator.optimistic_generation(self.device_url)
        return current != pending.generation

    def _log_expiry_once(self, field: str, pending: OptimisticValue) -> None:
        """Warn about an expired value, at most once per value."""
        if pending.expiry_logged:
            return
        pending.expiry_logged = True
        LOGGER.warning(
            "Optimistic %s for %s expired unconfirmed after %.0fs "
            "(wanted %s); falling back to device state",
            field,
            self.device_url,
            pending.age,
            pending.value,
        )

    def _queue_optimistically(
        self,
        field: str,
        value: Any,
        commands: list[Command],
        *,
        needs_mode_refresh: bool = False,
    ) -> None:
        """Assume a value, write it, and queue the commands that realise it.

        The optimistic value is written before queuing so the UI never
        flickers. If queuing itself fails the assumption is withdrawn straight
        away and the error reaches the service caller; failures that happen
        later, during the batched send, arrive via the coordinator's
        invalidation generation instead.
        """
        self._set_optimistic(field, value)
        self.async_write_ha_state()

        try:
            self.coordinator.queue_commands(
                self.device_url, commands, needs_mode_refresh=needs_mode_refresh
            )
        except Exception as exception:
            self._clear_optimistic(field)
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Could not queue {field} for {self.device_url}: {exception}"
            ) from exception

    def _reconcile_optimistic(
        self, confirmations: Mapping[str, Callable[[Any], bool]]
    ) -> None:
        """Drop assumed values the device has now confirmed or refuted.

        ``confirmations`` maps each field to a predicate that receives the
        assumed value and reports whether device state now matches it.
        """
        for field, is_confirmed in confirmations.items():
            pending = self._optimistic.get(field)
            if pending is None:
                continue

            if self._is_invalidated(pending):
                LOGGER.warning(
                    "Rolling back optimistic %s=%s for %s: command failed",
                    field,
                    pending.value,
                    self.device_url,
                )
                self._clear_optimistic(field)
                continue

            if is_confirmed(pending.value):
                LOGGER.debug(
                    "Optimistic %s=%s confirmed for %s after %.1fs",
                    field,
                    pending.value,
                    self.device_url,
                    pending.age,
                )
                self._clear_optimistic(field)
                continue

            if pending.is_expired():
                self._log_expiry_once(field, pending)
                self._clear_optimistic(field)

    def optimistic_diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return the current optimistic values, for diagnostics."""
        return {
            field: {
                "value": pending.value,
                "generation": pending.generation,
                "age": round(pending.age, 1),
            }
            for field, pending in self._optimistic.items()
        }
