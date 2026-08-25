from pydoover.tags import Tag, Tags


class FlowMeterTags(Tags):
    # --- Published, live values (consumed by other apps via get_tag) ---
    flow_rate = Tag("number", default=0.0, live=True)
    totaliser = Tag("number", default=0.0, live=True)

    # --- Raw / diagnostic ---
    # analog: raw mA/V reading
    raw_signal = Tag("number", default=None)
    # pulse: lifetime pulse counter (restart-safe)
    pulse_count = Tag("number", default=0)
    # pulse: counter value at the last totaliser reset
    pulse_offset = Tag("number", default=0)
    # epoch seconds of the last pulse
    last_pulse_dt = Tag("number", default=None)
    # pulse (hardware-counter source): the last raw counter value read off the
    # device. Persisted so a restart can credit the pulses that arrived while
    # this app was down - the whole point of counting on the device rather than
    # in here. Distinct from pulse_count, which stays this app's own monotonic
    # lifetime tally and is what the totaliser is derived from.
    hw_pulse_count = Tag("number", default=None)
    # analog: drives the "signal out of range" warning (True = hidden / healthy)
    sensor_fault_hidden = Tag("boolean", default=True)

    # --- Flow-session / event tracking ---
    flow_active = Tag("boolean", default=False)
    # epoch milliseconds the active session began (bound to a ui.Timestamp)
    event_started = Tag("number", default=None)
    # volume accumulated in the active session
    event_volume = Tag("number", default=0.0)
    # peak flow rate during the active session
    event_peak_flow = Tag("number", default=0.0)
    # human-readable summary of the last closed session
    last_event_summary = Tag("string", default="")
