
# Analog Flow Meter

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="App Icon" style="max-width: 300px;">

**Read flow from an analog input or by counting digital pulses, with a calibratable totaliser and flow-event tracking.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/analog-flow-meter/blob/main/LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/getdoover/analog-flow-meter?quickstart=1)

[Getting Started](#-getting-started) • [Configuration](#configuration) • [Developer](https://github.com/getdoover/analog-flow-meter/blob/main/DEVELOPMENT.md) • [Need Help?](#need-help)

<br/>

## 📖 Overview

A Doover device application for flow meters, built on [pydoover](https://github.com/getdoover/pydoover) 1.0.
It supports two measurement paradigms, selectable by config:

- **Analog** — reads an analog input pin and linearly scales the raw signal
  (e.g. 4-20mA or 0-10V) to a flow rate. The totaliser integrates the rate over time.
- **Pulse** — counts pulses on a digital input pin and converts them to volume
  using a K-factor (pulses per unit). The flow rate is derived from the pulse rate.

The flow rate and totaliser are published as **live** tags for other apps to
consume, the main radial gauge is calibrated to your configured maximum flow,
and discrete **flow sessions** (events) are tracked on a separate UI tab.

<br/>

## 🚀 Getting Started

This Doover App can be managed via the Doover CLI and installed onto devices
through the Doover platform. To iterate locally with a simulated flow signal
(no hardware required), set a **Simulator App Key** in config and run:

```bash
doover app run     # runs the app + flow simulator via docker-compose
```

### Configuration

Configuration fields are declared in [`src/analog_flow_meter/app_config.py`](src/analog_flow_meter/app_config.py).

**Common**

| Setting | Description | Default |
|---------|-------------|---------|
| **Meter Mode** | `Analog` (read a pin) or `Pulse` (count pulses) | `Analog` |
| **Flow Units** | Volume units for flow & totaliser (L, kL, gal, m3…) | `L` |
| **Flow Rate Time Base** | Time base for the displayed rate (e.g. Per Hour → L/hr) | `Per Hour` |
| **Maximum Flow** | Full-scale flow used to calibrate the radial gauge | `1000` |
| **Polling Frequency** | How often to sample the meter (Hz) | `1.0` |
| **Totaliser Calibration Factor** | Multiplier to trim the totaliser to a reference | `1.0` |

**Analog mode** — `Analog Input Pin`, `Signal at Minimum/Maximum Flow` (e.g. 4 / 20 mA),
`Flow at Minimum/Maximum Signal`, `Signal Deadband`, optional `Sensor Power Pin`.

**Pulse mode** — `Digital Input Pin`, `K-Factor (pulses per unit)`, `Pulse Edge`.

**Events** — `Event Flow Threshold` (rate above which a session is active) and
`Event Timeout (minutes)` (no-flow duration before a session closes).

After changing the schema, regenerate `doover_config.json` with `uv run export-config`
and `uv run export-ui`.

<br/>

## 🔗 Integrations

### Tags

Published via [`src/analog_flow_meter/app_tags.py`](src/analog_flow_meter/app_tags.py):

| Tag | Description |
|-----|-------------|
| **flow_rate** | Current flow rate (units per time base) — *live* |
| **totaliser** | Cumulative volume in flow units — *live* |
| **flow_active** | Whether a flow session is currently in progress |
| **event_volume** | Volume accumulated in the active session |
| **last_event_summary** | Human-readable summary of the last completed session |

`flow_rate` and `totaliser` are live tags; other apps can read them with
`get_tag("flow_rate", "<this-app-key>")`. Completed flow sessions are also
posted to the `significantEvent` channel.

<br/>

### Need Help?

- 📧 Email: support@doover.com
- 📖 [Doover Documentation](https://docs.doover.com)
- 👨‍💻 [App Developer Documentation](https://github.com/getdoover/analog-flow-meter/blob/main/DEVELOPMENT.md)

<br/>

## 📄 License

This app is licensed under the [Apache License 2.0](https://github.com/getdoover/analog-flow-meter/blob/main/LICENSE).
