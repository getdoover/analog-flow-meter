from pathlib import Path

from pydoover import config
from pydoover.config import ApplicationPosition


class MeterMode:
    ANALOG = "Analog"
    PULSE = "Pulse"


class TimeBase:
    """Flow-rate time bases and how many seconds each represents.

    The displayed flow rate is *volume per time base* (e.g. L/hr). Internally
    the app always integrates in volume-per-second, then scales to the
    configured time base for display.
    """

    SECOND = "Per Second"
    MINUTE = "Per Minute"
    HOUR = "Per Hour"
    DAY = "Per Day"

    SECONDS = {SECOND: 1.0, MINUTE: 60.0, HOUR: 3600.0, DAY: 86400.0}


class FlowMeterConfig(config.Schema):
    # --- Meter mode ---
    mode = config.Enum(
        "Meter Mode",
        choices=[MeterMode.ANALOG, MeterMode.PULSE],
        default=MeterMode.ANALOG,
        description="How flow is measured: an analog input (e.g. 4-20mA / 0-10V) "
        "or by counting digital pulses.",
    )

    # --- Units & display (common to both modes) ---
    units = config.String(
        "Flow Units",
        default="L",
        description="Volume units for flow and totaliser (e.g. L, kL, gal, m3).",
    )
    rate_time_base = config.Enum(
        "Flow Rate Time Base",
        choices=[TimeBase.SECOND, TimeBase.MINUTE, TimeBase.HOUR, TimeBase.DAY],
        default=TimeBase.HOUR,
        description="Time base for the displayed flow rate (e.g. Per Hour shows L/hr, "
        "Per Day shows ML/day for megalitre units).",
    )
    max_flow = config.Number(
        "Maximum Flow",
        default=1000.0,
        minimum=0,
        description="Full-scale flow used to calibrate the radial gauge range, "
        "in flow units per the selected time base.",
    )
    flow_precision = config.Integer(
        "Flow Decimal Precision",
        default=1,
        minimum=0,
        description="Number of decimal places shown for the flow rate.",
    )
    poll_frequency = config.Number(
        "Polling Frequency",
        default=1.0,
        minimum=0,
        description="How often to sample the meter and update the totaliser (Hz).",
    )

    # --- Analog mode (generic linear scaling) ---
    ai_pin = config.Integer(
        "Analog Input Pin",
        default=0,
        minimum=0,
        description="[Analog mode] Analog input pin the flow transmitter is wired to.",
    )
    signal_min = config.Number(
        "Signal at Minimum Flow",
        default=4.0,
        description="[Analog mode] Raw signal (mA or V) corresponding to the minimum flow.",
    )
    signal_max = config.Number(
        "Signal at Maximum Flow",
        default=20.0,
        description="[Analog mode] Raw signal (mA or V) corresponding to the maximum flow.",
    )
    flow_at_signal_min = config.Number(
        "Flow at Minimum Signal",
        default=0.0,
        description="[Analog mode] Flow rate (units per time base) at the minimum signal.",
    )
    flow_at_signal_max = config.Number(
        "Flow at Maximum Signal",
        default=1000.0,
        description="[Analog mode] Flow rate (units per time base) at the maximum signal.",
    )
    signal_deadband = config.Number(
        "Signal Deadband",
        default=0.5,
        minimum=0,
        description="[Analog mode] Signal below (signal_min - deadband) is treated as a "
        "fault/disconnected and ignored rather than read as flow.",
    )
    power_pin = config.Integer(
        "Sensor Power Pin",
        default=None,
        minimum=0,
        description="[Analog mode] Optional digital output pin used to power the transmitter.",
    )

    # --- Pulse mode (K-factor) ---
    di_pin = config.Integer(
        "Digital Input Pin",
        default=0,
        minimum=0,
        description="[Pulse mode] Digital input pin the meter's pulse output is wired to. "
        "Pins 4 and 5 are voltage-input (VI) pulse counters wired to analog inputs 0 "
        "and 1: the firmware polls the voltage and emits a pulse on each step change "
        "(see Voltage Pulse Threshold), rather than counting a hardware digital edge.",
    )
    k_factor = config.Number(
        "K-Factor (pulses per unit)",
        default=1.0,
        minimum=0,
        description="[Pulse mode] Pulses emitted per one unit of volume, from the "
        "meter's spec sheet. Volume = pulses / K-factor.",
    )
    pulse_edge = config.Enum(
        "Pulse Edge",
        choices=["rising", "falling"],
        default="rising",
        description="[Pulse mode] Signal edge that constitutes one pulse.",
    )
    vi_pulse_threshold = config.Number(
        "Voltage Pulse Threshold",
        default=10.0,
        minimum=0,
        description="[Pulse mode, DI pins 4-5 only] Voltage step (volts) between "
        "consecutive ~0.5s samples that registers as one pulse on a VI pulse counter. "
        "Rising vs falling step follows the Pulse Edge setting. Set below your pulse "
        "output's voltage swing (e.g. ~10 for a 0/24V pulse).",
    )
    vi_poll_rate = config.Number(
        "VI Poll Rate",
        default=0.4,
        minimum=0.05,
        description="[Pulse mode, DI pins 4-5 only] How often (seconds) the firmware "
        "samples the voltage input to detect pulses. Lower captures shorter/faster "
        "pulses (default 0.4s; e.g. 0.1 for 100ms). Requires platform firmware that "
        "supports a configurable VI poll rate; ignored otherwise (falls back to 0.4s).",
    )
    pulse_rate_window = config.Number(
        "Pulse Rate Averaging Window",
        default=10.0,
        minimum=0,
        description="[Pulse mode] Seconds of pulses to average when computing the "
        "displayed flow rate. Larger values smooth a jumpy gauge at low flow; "
        "smaller values respond faster. Does not affect the totaliser.",
    )

    # --- Totaliser ---
    totaliser_calibration = config.Number(
        "Totaliser Calibration Factor",
        default=1.0,
        minimum=0,
        description="Multiplier applied to the totaliser to trim it against a "
        "reference meter (1.0 = no adjustment).",
    )
    totaliser_precision = config.Integer(
        "Totaliser Decimal Precision",
        default=0,
        minimum=0,
        description="Decimal places shown for the totaliser and event volumes. "
        "Increase for small-volume units like m3 or kL.",
    )

    # --- Event tracking (flow sessions) ---
    event_flow_threshold = config.Number(
        "Event Flow Threshold",
        default=1.0,
        minimum=0,
        description="Flow rate (units per time base) above which a flow session is "
        "considered active. Set this above your sensor's zero/noise floor — with "
        "analog sensors, leaving it at 0 keeps a session open forever on baseline "
        "noise (flow rarely reads exactly zero).",
    )
    event_timeout = config.Number(
        "Event Timeout (minutes)",
        default=5.0,
        minimum=0,
        description="Minutes of flow at or below the threshold before an active flow "
        "session is closed and recorded.",
    )

    # --- Simulation / integration ---
    sim_app_key = config.Application(
        "Simulator App Key",
        default=None,
        description="Optional: app key of a simulator publishing a 'sim_flow_rate' tag. "
        "When set, flow is read from that tag instead of hardware (local testing).",
    )

    position = ApplicationPosition()


def export():
    FlowMeterConfig.export(
        Path(__file__).parents[2] / "doover_config.json",
        "analog_flow_meter",
    )


if __name__ == "__main__":
    export()
