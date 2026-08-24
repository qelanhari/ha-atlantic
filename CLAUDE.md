# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Custom Home Assistant integration for the **Atlantic Zone Control 2.0** (Pass APC) heat pump, connected via a **Somfy TaHoma Switch**. Communicates with the Overkiz cloud API using `pyoverkiz>=2.1.0,<3.0.0`. The pin is a **range, not `==`**: HA core's own Overkiz integration installs `pyoverkiz[nexity]` into the same interpreter, and an exact pin that disagrees with core's is unsatisfiable. Requires HA ≥ 2026.8.0. Multi-device batching uses the public `OverkizClient.execute_action_group(actions=[Action(...)])` API.

Distributed via HACS. Source lives entirely in `custom_components/atlantic_zone_control/`.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt "pyoverkiz>=2.1.0,<3.0.0" ruff
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check custom_components/ tests/
```

Note the test harness (`pytest-homeassistant-custom-component`) pins an older HA than the
integration's declared minimum; the code under test is version-agnostic, but be aware of the gap.

To verify against real hardware:
1. Copy `custom_components/atlantic_zone_control/` into a Home Assistant instance's `config/custom_components/`
2. Restart Home Assistant (or reload the integration)
3. Verify entity behavior in the HA UI / developer tools

Version is managed manually in `manifest.json`. Release flow: bump version → commit → push → `gh release create vX.Y.Z`.

## Architecture

```
Config Flow → __init__.py → Coordinator → Climate Entities
                                ↕              ↕
                          Overkiz Cloud    Executor (state/command helpers)
```

**Coordinator** (`coordinator.py`) — Central hub. Polls Overkiz events every 90s (2s during active executions). Routes events via `EVENT_HANDLERS` registry to update in-memory `Device` objects. Manages a command queue with a 2-second debounce, merging by command name so repeated +/- taps collapse to one command. Uses `OverkizBatchExecutor` for multi-device calls, with per-device fallback. Failures — send errors, `FAILED`/`NOT_TRANSMITTED` executions, and executions swept as stale — bump a per-device invalidation generation that entities read.

**Reconciliation** — The cloud is the source of truth. Events are the primary channel but are lost whenever the listener is silently re-registered or a poll fails, so `async_reconcile()` re-reads the states backing entities via `client.get_state()` every 15 minutes, after any `UpdateFailed` recovery, and whenever `client.event_listener_id` changes. **Do not use `get_devices(refresh=True)` for this** — it is rate-limited to roughly 1/day. Values are normalized through `EventState` before comparison: `get_state()` returns uncast values, and comparing those raw against event-sourced values would report a correction every cycle. Note this reads the *server* cache, so it cannot fix gateway↔server staleness.

**Command tracking** (`command_tracker.py`) — Maps `exec_id` → the devices whose commands it carries, so a failure invalidates exactly the right optimistic state. Every entry ages out (`EXECUTION_MAX_AGE`); without that, one execution that never terminates pins the 2s poll indefinitely. Foreign executions (Somfy app, wall thermostat) are tracked with *no* device URLs so they expire without touching our state.

**Two climate entity types** (`climate.py`):
- `AtlanticPassAPCZoneControl` — System-wide mode (Heat/Cool/Auto/Dry/Off). One per installation.
- `AtlanticPassAPCZoneControlZone` — Per-zone On/Off + temperature. Mode-aware: commands sent depend on whether the system is in heating or cooling mode. Reads from a linked zone control device (index `#1`) to determine current operating mode.

**Optimistic state** (`optimistic.py`) — `OptimisticStateMixin` holds an assumed value so the UI doesn't flicker during the debounce + execution window. Each value is held until **exactly one** of four things happens, never longer:

1. Real device state confirms it (modes by equality, temperature within `TEMPERATURE_TOLERANCE`, and only when the device reports a value at all).
2. Queuing raised — rolled back and re-raised as `HomeAssistantError`.
3. The coordinator bumped this device's invalidation generation (execution failed or was swept).
4. `OPTIMISTIC_MAX_AGE` (200s) elapsed. This must exceed one full restored poll cycle, since the fast poll un-pins on `COMPLETED` and a slow zone's confirming event can arrive up to `UPDATE_INTERVAL` later.

Expiry is evaluated **on read** as well as on coordinator update, so a value cannot outlive its deadline even if the coordinator stops firing. Optimistic state is deliberately **kept** across a transient `UpdateFailed`: a poll blip is not evidence of failure, and clearing there reintroduces flicker.

**Entity base** (`entity.py`) — `OverkizEntity` extends `CoordinatorEntity`. `available` tolerates a device vanishing from coordinator data, which a `ServerDisconnectedError` resync can cause.

**Executor** (`executor.py`) — Helper wrapping a device URL. Provides `select_state()`, `has_command()`, `linked_device()` for navigating the Overkiz device tree.

**Command flow**: `climate.async_set_*()` → `_queue_optimistically()` → `coordinator.queue_commands()` → 2s debounce → `_async_flush_commands()` → batch or per-device API call → `tracker.register()` → `async_refresh()` → event polling confirms or reports failure.

## Key Conventions

- Device URLs use `#` indexing (e.g., `base_url#1` for zone control, `base_url#N` for zones)
- Zone control device is always at index `#1`; temperature sensors at zone index + 1
- Skip-if-unchanged compares against **real** state, never the optimistic value, and never skips while `has_pending_commands()` — comparing against our own assumption is what used to make a wrong assumption permanent
- Commands impossible in the current system mode (`stop`/`drying`) raise `ServiceValidationError` rather than silently succeeding
- `needs_mode_refresh=True` triggers a follow-up refresh of heating/cooling mode states 2s after flush
- `_real_*` properties read from device state; public properties check optimistic first
- `time.monotonic` must be called, not bound as a `default_factory` — a bound reference escapes test clock patching
