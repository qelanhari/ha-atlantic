"""Tests for execution tracking."""

from __future__ import annotations

import time

from custom_components.atlantic_zone_control.command_tracker import CommandTracker


def test_register_and_pop() -> None:
    """A registered execution can be recovered with its devices."""
    tracker = CommandTracker()
    tracker.register("exec-1", ["dev#1"], ["setHeatingOnOffState"])

    pending = tracker.pop("exec-1")

    assert pending is not None
    assert pending.device_urls == frozenset({"dev#1"})
    assert pending.command_names == ("setHeatingOnOffState",)
    assert not tracker


def test_register_merges_same_exec_id() -> None:
    """The server may merge concurrent action groups under one exec_id."""
    tracker = CommandTracker()
    tracker.register("exec-1", ["dev#1"], ["a"])
    tracker.register("exec-1", ["dev#2"], ["b"])

    pending = tracker.pop("exec-1")

    assert pending is not None
    assert pending.device_urls == frozenset({"dev#1", "dev#2"})
    assert pending.command_names == ("a", "b")


def test_foreign_execution_carries_no_devices() -> None:
    """A Somfy-app execution must never invalidate our optimistic state."""
    tracker = CommandTracker()
    tracker.register_foreign("exec-foreign")

    pending = tracker.pop("exec-foreign")

    assert pending is not None
    assert pending.device_urls == frozenset()


def test_foreign_registration_does_not_clobber_ours() -> None:
    """An ExecutionRegistered echo must not erase the devices we recorded."""
    tracker = CommandTracker()
    tracker.register("exec-1", ["dev#1"], ["a"])
    tracker.register_foreign("exec-1")

    pending = tracker.pop("exec-1")

    assert pending is not None
    assert pending.device_urls == frozenset({"dev#1"})


def test_sweep_expires_stale_executions() -> None:
    """An execution with no terminal state must not pin the fast poll."""
    tracker = CommandTracker()
    tracker.register("exec-1", ["dev#1"], ["a"])
    tracker.register("exec-2", ["dev#2"], ["b"])
    tracker._pending["exec-1"].updated_at = time.monotonic() - 500

    stale = tracker.sweep(180.0)

    assert [p.exec_id for p in stale] == ["exec-1"]
    assert len(tracker) == 1


def test_touch_resets_age() -> None:
    """Progress on a slow execution defers the sweep."""
    tracker = CommandTracker()
    tracker.register("exec-1", ["dev#1"], ["a"])
    tracker._pending["exec-1"].updated_at = time.monotonic() - 500

    tracker.touch("exec-1")

    assert tracker.sweep(180.0) == []
