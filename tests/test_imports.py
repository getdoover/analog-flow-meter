"""Tests for the analog flow meter application.

These validate that modules import, the config/UI schemas are well-formed and
export end-to-end, and that the core flow maths (analog scaling, pulse
totalising, session timing) behave as expected.
"""

import asyncio
import json

from pydoover.config import Schema
from pydoover.tags import Tags
from pydoover.ui import UI


def test_import_app():
    from analog_flow_meter.application import FlowMeterApplication

    assert FlowMeterApplication.config_cls is not None
    assert FlowMeterApplication.tags_cls is not None
    assert FlowMeterApplication.ui_cls is not None


def test_config_schema():
    from analog_flow_meter.app_config import FlowMeterConfig

    assert issubclass(FlowMeterConfig, Schema)

    schema = FlowMeterConfig.to_schema()
    assert isinstance(schema, dict)
    assert schema["type"] == "object"

    props = schema["properties"]
    # Mode selector plus a field from each paradigm and the calibration knobs.
    for key in (
        "meter_mode",
        "analog_input_pin",
        "signal_at_minimum_flow",
        "kfactor_pulses_per_unit",
        "maximum_flow",
        "totaliser_calibration_factor",
        "event_timeout_minutes",
    ):
        assert key in props, f"missing config field: {key}"

    # Sensible defaults: litres, per-hour rate, analog by default.
    assert props["flow_units"]["default"] == "L"
    assert props["flow_rate_time_base"]["default"] == "Per Hour"
    assert props["meter_mode"]["default"] == "Analog"

    # Rate smoothing + configurable totaliser precision are present.
    assert "pulse_rate_averaging_window" in props
    assert "totaliser_decimal_precision" in props

    # Event threshold must default above 0 so analog sessions can actually
    # close (baseline noise rarely reads exactly zero).
    assert props["event_flow_threshold"]["default"] > 0


def test_tags_published_live():
    from analog_flow_meter.app_tags import FlowMeterTags

    assert issubclass(FlowMeterTags, Tags)

    # flow_rate and totaliser must be live (streamed) for cross-app integration.
    assert FlowMeterTags.flow_rate.live is True
    assert FlowMeterTags.totaliser.live is True


def test_ui_structure():
    from analog_flow_meter.app_ui import FlowMeterUI

    assert issubclass(FlowMeterUI, UI)

    schema = FlowMeterUI(None, None, None).to_schema(resolve_config=False)
    tabs = schema["children"]["tabs"]
    assert tabs["type"] == "uiTabs"
    assert "flow_tab" in tabs["children"]
    assert "events_tab" in tabs["children"]

    flow_children = tabs["children"]["flow_tab"]["children"]
    assert flow_children["flow_rate"]["form"] == "radialGauge"
    # Analog fault surfaces as a warning indicator on the Flow tab.
    assert flow_children["sensor_fault"]["type"] == "uiWarningIndicator"


def test_state_machine_starts_idle():
    from analog_flow_meter.app_state import FlowSessionState

    session = FlowSessionState()
    assert session.state == "idle"
    assert session.flowing is False


def test_state_machine_transitions():
    from analog_flow_meter.app_state import FlowSessionState

    session = FlowSessionState()
    asyncio.run(session.start_flow())
    assert session.flowing is True

    asyncio.run(session.stop_flow())
    assert session.flowing is False


def test_scale_analog_linear():
    from analog_flow_meter.application import FlowMeterApplication

    scale = FlowMeterApplication.scale_analog
    # 4-20mA -> 0-1000: endpoints and midpoint.
    assert scale(4, 4, 20, 0, 1000) == 0
    assert scale(20, 4, 20, 0, 1000) == 1000
    assert scale(12, 4, 20, 0, 1000) == 500


def test_scale_analog_clamps_out_of_band():
    from analog_flow_meter.application import FlowMeterApplication

    scale = FlowMeterApplication.scale_analog
    # Below the band clamps to min flow, above clamps to max flow.
    assert scale(2, 4, 20, 0, 1000) == 0
    assert scale(24, 4, 20, 0, 1000) == 1000
    # Degenerate calibration doesn't divide by zero.
    assert scale(10, 5, 5, 0, 1000) == 0


def test_pulse_rate_smoothing():
    """A steady low pulse rate should give a smooth gauge, not 0/spike flicker.

    1 pulse every 3s at K=1 (pulses/L), Per Hour -> ~1200 L/hr. A naive per-loop
    delta would swing between 0 and 3600 L/hr; the windowed average must not.
    """
    from collections import deque
    from types import SimpleNamespace

    from analog_flow_meter.application import FlowMeterApplication

    app = object.__new__(FlowMeterApplication)
    app._seconds_per_base = 3600.0
    app._loop_period = 1.0
    app._prev_pulse_count = 0
    app._pulse_samples = deque()

    val = lambda v: SimpleNamespace(value=v)  # noqa: E731
    app.config = SimpleNamespace(
        k_factor=val(1.0),
        totaliser_calibration=val(1.0),
        pulse_rate_window=val(10.0),
    )
    count = {"v": 0}
    app.tags = SimpleNamespace(
        pulse_count=SimpleNamespace(get=lambda: count["v"]),
        pulse_offset=SimpleNamespace(get=lambda: 0),
    )

    rates = []
    for sec in range(31):
        count["v"] = sec // 3  # one pulse every 3 seconds
        rate, _vol, _tot = app._read_pulse(float(sec), 1.0)
        rates.append(rate)

    tail = rates[15:]  # after the window has filled
    assert all(r > 0 for r in tail), "rate flickers to zero between pulses"
    assert max(tail) < 1600, "rate still spiking like a per-loop delta"
    assert 1000 < sum(tail) / len(tail) < 1400, "rate should average near 1200 L/hr"


def test_format_duration():
    from analog_flow_meter.application import FlowMeterApplication

    fmt = FlowMeterApplication._format_duration
    assert fmt(45) == "45s"
    assert fmt(90) == "1m 30s"
    assert fmt(3661) == "1h 1m"


def test_config_export(tmp_path):
    from analog_flow_meter.app_config import FlowMeterConfig

    fp = tmp_path / "doover_config.json"
    FlowMeterConfig.export(fp, "analog_flow_meter")

    data = json.loads(fp.read_text())
    assert "config_schema" in data["analog_flow_meter"]
    assert "properties" in data["analog_flow_meter"]["config_schema"]


def test_ui_export(tmp_path):
    from analog_flow_meter.app_ui import FlowMeterUI

    fp = tmp_path / "doover_config.json"
    FlowMeterUI(None, None, None).export(fp, "analog_flow_meter")

    data = json.loads(fp.read_text())
    ui_schema = data["analog_flow_meter"]["ui_schema"]
    assert ui_schema["type"] == "uiApplication"
    assert "tabs" in ui_schema["children"]
