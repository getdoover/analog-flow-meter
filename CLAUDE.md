# Analog Flow Meter

A Doover device app (built on pydoover 1.0) that reads flow from either an
**analog input** (generic linear scaling, e.g. 4-20mA / 0-10V) or by **counting
digital pulses** (K-factor, pulses per unit). It publishes a live flow rate and
a calibratable totaliser, and tracks discrete **flow sessions** (events).

## Commands

```bash
uv run pytest tests -v          # Run tests
uv run export-config             # Write config_schema into doover_config.json
uv run export-ui                 # Write ui_schema into doover_config.json (required to publish)
doover app run                   # Run app + simulator locally via docker-compose
```

## Project Structure

```
src/analog_flow_meter/
  __init__.py        # Entry point — run_app(FlowMeterApplication())
  application.py     # Main app: reads analog/pulse flow, totalises, tracks sessions
  app_config.py      # Config schema — mode, units, calibration, pins (class-level)
  app_tags.py        # Tags — flow_rate & totaliser are live + published
  app_ui.py          # UI — TabContainer: "Flow" (radial gauge) + "Events" tabs
  app_state.py       # FlowSessionState (idle ↔ flowing) via pydoover.state.StateMachine
simulators/sample/   # Flow simulator publishing a `sim_flow_rate` tag for local dev
tests/               # pytest suite (schema, UI, scaling/totaliser maths)
```

## How it works

- **Mode** (`meter_mode` config) selects Analog or Pulse.
- **Analog**: `platform_iface.fetch_ai(pin)` → linear scale (`scale_analog`) between
  configured signal/flow endpoints → flow rate; the totaliser **integrates**
  `rate × dt`.
- **Pulse**: `platform_iface.start_di_pulse_listener(pin, on_pulse, edge)` keeps a
  lifetime `pulse_count`; the totaliser is **derived** as
  `(pulse_count − pulse_offset) / k_factor × calibration`. Missed pulses are
  recovered on restart via `fetch_di_events`.
- **Rate display** is volume per configurable time base (default `L/hr`); the
  radial gauge range is calibrated to `maximum_flow` at runtime in `UI.setup()`.
- **Simulation**: when `simulator_app_key` is set, flow is read from that app's
  `sim_flow_rate` tag instead of hardware (used by `doover app run`).
- **Events**: a flow session opens when rate exceeds `event_flow_threshold` and
  closes after `event_timeout` minutes below it; closed sessions publish a
  message to the `significantEvent` channel.

## pydoover 1.0 Patterns

This app uses the pydoover 1.0 declarative API. Key patterns:

### Application class (application.py)
- Set `config_cls`, `tags_cls`, `ui_cls` as class attributes — framework wires them up automatically
- Override `async def setup()` for init and `async def main_loop()` for the periodic loop
- Use `@ui.handler("element_name")` for UI interaction callbacks (signature: `self, ctx, value`)
- Access config via `self.config.<field>.value`, tags via `self.tags.<name>.set(val)` / `.get()`
- Cross-app tags: `self.get_tag("tag_name", app_key)`
- Messaging: `await self.create_message(channel, {data})`

### Config (app_config.py)
- Subclass `config.Schema` with class-level `config.Boolean`, `config.String`, `config.Application`, etc.
- `export()` is a classmethod: `SampleConfig.export(path, name)`

### Tags (app_tags.py)
- Subclass `Tags` with class-level `Tag("type", default=...)` declarations
- Types: "boolean", "number", "integer", "string", "array", "object"

### UI (app_ui.py)
- Subclass `ui.UI` with class-level element declarations
- Bind variables to tags: `ui.NumericVariable("Label", value=MyTags.field, name="id")`
- Element types: `BooleanVariable`, `NumericVariable`, `TextVariable`, `Button`, `TextInput`, `FloatInput`, `Select`, `Submodule`
- Use explicit `name=` kwarg on interactive elements to match handler names

### State Machine (app_state.py)
- Uses `pydoover.state.StateMachine` (wraps the `transitions` library)
- Define `states` and `transitions` as class attributes, `on_enter_<state>()` callbacks

## Doover Skills

If you have the doover-skills plugin installed, use `/doover` to see all available skills.
Key skills: `/doover-device-apps` for device app development, `/pydoover` for API reference.
