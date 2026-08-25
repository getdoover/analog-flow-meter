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


def test_resolve_pulse_source_vi_mapping():
    """DI pins 4/5 route to VI pulse counting on AI pins 0/1 with a threshold edge.

    Plain DI pins pass straight through with their rising/falling edge.
    """
    from types import SimpleNamespace

    from analog_flow_meter.application import FlowMeterApplication

    val = lambda v: SimpleNamespace(value=v)  # noqa: E731

    def resolve(di_pin, edge, threshold=10.0, poll=0.4):
        app = object.__new__(FlowMeterApplication)
        app.config = SimpleNamespace(
            di_pin=val(di_pin),
            pulse_edge=val(edge),
            vi_pulse_threshold=val(threshold),
            vi_poll_rate=val(poll),
        )
        return app._resolve_pulse_source()

    # Plain DI edge counter: pin and edge unchanged, not VI.
    assert resolve(0, "rising") == (0, "rising", False)
    assert resolve(2, "falling") == (2, "falling", False)

    # DI 4/5 -> AI 0/1, edge encodes the signed threshold, flagged as VI.
    # Default poll rate (0.4) omits the suffix for backward compatibility.
    assert resolve(4, "rising", 10.0) == (0, "VI+10.0", True)
    assert resolve(5, "falling", 9.5) == (1, "VI-9.5", True)

    # A non-default poll rate rides in the edge string as an "@<seconds>" suffix.
    assert resolve(4, "rising", 10.0, poll=0.1) == (0, "VI+10.0@0.1", True)
    assert resolve(5, "falling", 9.5, poll=0.05) == (1, "VI-9.5@0.05", True)


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


# --- Hardware pulse counter source -----------------------------------------


def _counter_app(pulse_source, readings=None, raise_with=None, is_vi=False):
    """A FlowMeterApplication wired to a canned platform interface."""
    from types import SimpleNamespace

    from analog_flow_meter.application import FlowMeterApplication

    app = object.__new__(FlowMeterApplication)
    app.config = SimpleNamespace(pulse_source=SimpleNamespace(value=pulse_source))

    class FakeIface:
        def __init__(self):
            self.calls = []

        async def fetch_di_readings(self, pin):
            self.calls.append(pin)
            if raise_with is not None:
                raise raise_with
            return readings

    app.platform_iface = FakeIface()
    return app


def _reading(pin, value=True, pulse_count=None, pulse_rate_hz=None):
    from pydoover.docker.platform.platform_types import DIReading

    return DIReading(
        pin=pin, value=value, pulse_count=pulse_count, pulse_rate_hz=pulse_rate_hz
    )


def test_auto_uses_the_hardware_counter_when_the_pin_has_one():
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.AUTO, readings=[_reading(0, pulse_count=41233)])
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is True


def test_auto_falls_back_to_events_when_the_pin_cannot_count():
    """A Doovit VI pin, or any platform without counters, still measures flow."""
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.AUTO, readings=[_reading(0, pulse_count=None)])
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is False


def test_a_zero_count_still_counts_as_supported():
    """A brand new meter reads 0 pulses; that is a counter, not the absence of one."""
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.AUTO, readings=[_reading(0, pulse_count=0)])
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is True


def test_vi_pins_never_use_the_hardware_counter():
    """A VI pulse is a voltage step the firmware polls for, not a digital edge."""
    from analog_flow_meter.app_config import PulseSource

    for source in (PulseSource.AUTO, PulseSource.COUNTER, PulseSource.EVENTS):
        app = _counter_app(source, readings=[_reading(0, pulse_count=99)])
        assert asyncio.run(app._resolve_counter_source(0, is_vi=True)) is False
        # It must not even probe: on a VI pin the answer is structural.
        assert app.platform_iface.calls == []


def test_events_is_honoured_without_probing():
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.EVENTS, readings=[_reading(0, pulse_count=5)])
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is False
    assert app.platform_iface.calls == []


def test_counter_forced_but_unavailable_falls_back():
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.COUNTER, readings=[_reading(0, pulse_count=None)])
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is False


def test_a_probe_failure_does_not_break_setup():
    """An unreachable platform interface must not stop the app starting."""
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.AUTO, raise_with=RuntimeError("iface down"))
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is False


def test_old_pydoover_without_fetch_di_readings_falls_back():
    from analog_flow_meter.app_config import PulseSource

    app = _counter_app(PulseSource.AUTO, raise_with=AttributeError("no such method"))
    assert asyncio.run(app._resolve_counter_source(0, is_vi=False)) is False


def test_counter_delta_normal_progress():
    from analog_flow_meter.application import FlowMeterApplication

    assert FlowMeterApplication._counter_delta(100, 137) == 37
    assert FlowMeterApplication._counter_delta(100, 100) == 0


def test_counter_delta_first_poll_has_no_baseline():
    """A fresh install must not import the device's lifetime total as flow."""
    from analog_flow_meter.application import FlowMeterApplication

    assert FlowMeterApplication._counter_delta(None, 41233) == 0


def test_counter_delta_survives_a_u32_wrap():
    from analog_flow_meter.application import FlowMeterApplication

    assert FlowMeterApplication._counter_delta(2**32 - 3, 2) == 5


def test_counter_delta_treats_a_device_restart_as_zero():
    """Crediting 4 billion phantom pulses would be far worse than losing a few."""
    from analog_flow_meter.application import FlowMeterApplication

    assert FlowMeterApplication._counter_delta(5000, 3) == 0


def test_hardware_counter_credits_pulses_missed_while_down():
    """The point of counting on the device: an outage costs nothing.

    The app stops at count 1000, the meter turns another 250 pulses, and the app
    comes back. The first poll must credit all 250.
    """
    from types import SimpleNamespace

    from analog_flow_meter.application import FlowMeterApplication

    app = object.__new__(FlowMeterApplication)
    app._pulse_pin = 1
    app._prev_hw_count = 1000  # restored from the hw_pulse_count tag
    store = {"pulse_count": 400, "hw": 1000, "last_dt": None}

    async def fetch(pin):
        return [_reading(1, pulse_count=1250)]

    app.platform_iface = SimpleNamespace(fetch_di_readings=fetch)

    def setter(key):
        async def _set(v):
            store[key] = v

        return _set

    app.tags = SimpleNamespace(
        pulse_count=SimpleNamespace(get=lambda: store["pulse_count"], set=setter("pulse_count")),
        hw_pulse_count=SimpleNamespace(get=lambda: store["hw"], set=setter("hw")),
        last_pulse_dt=SimpleNamespace(set=setter("last_dt")),
    )

    asyncio.run(app._poll_hardware_counter())

    # Lifetime count advanced by the full 250, not reset to the device's value.
    assert store["pulse_count"] == 650
    assert store["hw"] == 1250
    assert store["last_dt"] is not None


def test_hardware_counter_holds_steady_when_the_count_vanishes():
    """A platform that stops reporting a count must not zero the totaliser."""
    from types import SimpleNamespace

    from analog_flow_meter.application import FlowMeterApplication

    app = object.__new__(FlowMeterApplication)
    app._pulse_pin = 0
    app._prev_hw_count = 500
    store = {"pulse_count": 120}

    async def fetch(pin):
        return [_reading(0, pulse_count=None)]

    app.platform_iface = SimpleNamespace(fetch_di_readings=fetch)
    app.tags = SimpleNamespace(
        pulse_count=SimpleNamespace(
            get=lambda: store["pulse_count"],
            set=lambda v: (_ for _ in ()).throw(AssertionError("must not write")),
        ),
    )

    asyncio.run(app._poll_hardware_counter())
    assert store["pulse_count"] == 120
    # The baseline is untouched, so the next good read measures from 500.
    assert app._prev_hw_count == 500
