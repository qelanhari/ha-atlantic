# Atlantic Zone Control for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/v/release/qelanhari/ha-atlantic)](https://github.com/qelanhari/ha-atlantic/releases)

Custom Home Assistant integration for the **Atlantic Zone Control 2.0** heat pump, connected via a **Somfy TaHoma Switch**.

Provides simplified climate control with per-zone temperature management and system-wide mode switching (heat/cool/auto/off).

## Features

- **System mode control** — Switch between Heat, Cool, Auto, Dry, and Off
- **Per-zone temperature** — Set individual target temperatures for each zone
- **Per-zone on/off** — Enable or disable heating/cooling per zone independently
- **Auto mode detection** — Zone commands adapt automatically based on current system mode (heating vs cooling)
- **Command batching** — Multiple commands sent in a single API call for reliability
- **Dynamic discovery** — Zones and sensors are detected automatically from your TaHoma setup

## Entities Created

| Entity | Type | Description |
|--------|------|-------------|
| Zone Control | Climate | System-wide mode: Heat / Cool / Auto / Dry / Off |
| Each Zone (e.g. Salon, Chambre...) | Climate | On/Off + single target temperature |

## Requirements

- Atlantic Zone Control 2.0 (Pass APC) heat pump
- Somfy TaHoma Switch (or compatible Overkiz gateway)
- Somfy account with cloud API access

## Installation

### HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Click the 3-dot menu → **Custom repositories**
3. Add `qelanhari/ha-atlantic` with category **Integration**
4. Search for "Atlantic Zone Control" and install it
5. Restart Home Assistant

### Manual

1. Copy `custom_components/atlantic_zone_control/` to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Atlantic Zone Control**
3. Enter your Somfy TaHoma Switch email and password
4. Your zones will be discovered automatically

## How It Works

### Zone Control (System Mode)

The zone control entity manages the global operating mode of the heat pump. Changing it affects how all zones behave:

- **Heat** — Zones use heating commands and heating target temperatures
- **Cool** — Zones use cooling commands and cooling target temperatures
- **Auto** — System switches between heating and cooling automatically
- **Off** — System is stopped

### Zone Entities

Each zone exposes two HVAC modes:

- **Auto** — Zone is active with manual temperature control (follows the system mode)
- **Off** — Zone is turned off

The target temperature command sent depends on the current system mode set on the zone control.

## Credits

Built on top of [pyoverkiz](https://github.com/iMicknl/python-overkiz-api) and inspired by the native [Home Assistant Overkiz integration](https://www.home-assistant.io/integrations/overkiz/).
