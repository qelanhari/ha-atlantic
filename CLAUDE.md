# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Custom Home Assistant integration for the **Atlantic Zone Control 2.0** (Pass APC) heat pump, connected via a **Somfy TaHoma Switch**. Communicates with the Overkiz cloud API using `pyoverkiz==1.20.0` (pinned for batch execution support via internal `__post` method).

Distributed via HACS. Source lives entirely in `custom_components/atlantic_zone_control/`.

## Development

No build/test/lint tooling is configured. To test changes:
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

**Coordinator** (`coordinator.py`) — Central hub. Polls Overkiz events every 90s (2s during active executions). Routes events via `EVENT_HANDLERS` registry to update in-memory `Device` objects. Manages command queue with 2-second debounce to batch rapid changes (e.g., repeated +/- temperature taps) into a single API call. Uses `OverkizBatchExecutor` for multi-device calls, with per-device fallback.

**Two climate entity types** (`climate.py`):
- `AtlanticPassAPCZoneControl` — System-wide mode (Heat/Cool/Auto/Dry/Off). One per installation.
- `AtlanticPassAPCZoneControlZone` — Per-zone On/Off + temperature. Mode-aware: commands sent depend on whether the system is in heating or cooling mode. Reads from a linked zone control device (index `#1`) to determine current operating mode.

**Optimistic state** — Both entity types set `_optimistic_hvac_mode` / `_optimistic_temperature` immediately when queuing commands, then call `async_write_ha_state()`. Optimistic values are cleared only when the real device state confirms the change (matches). This prevents UI flickering during the debounce + execution window.

**Entity base** (`entity.py`) — `OverkizEntity` extends `CoordinatorEntity`. Provides `async_refresh_if_stale()` (refreshes if data is >1s old) used before every command to avoid acting on stale state.

**Executor** (`executor.py`) — Helper wrapping a device URL. Provides `select_state()`, `has_command()`, `linked_device()` for navigating the Overkiz device tree.

**Command flow**: `climate.async_set_*()` → `coordinator.queue_commands()` → 2s debounce → `_async_flush_commands()` → batch or per-device API call → `async_refresh()` → event polling picks up state changes.

## Key Conventions

- Device URLs use `#` indexing (e.g., `base_url#1` for zone control, `base_url#N` for zones)
- Zone control device is always at index `#1`; temperature sensors at zone index + 1
- Skip-if-unchanged: all command methods check current real state before queuing
- `needs_mode_refresh=True` triggers a follow-up refresh of heating/cooling mode states 2s after flush
- `_real_*` properties read from device state; public properties check optimistic first
