import logging
import time
from collections import deque

from pydoover.docker import Application
from pydoover import ui

from .app_config import FlowMeterConfig, MeterMode, PulseSource, TimeBase
from .app_tags import FlowMeterTags
from .app_ui import FlowMeterUI, _TIME_BASE_ABBREV
from .app_state import FlowSessionState

log = logging.getLogger(__name__)

# Firmware default VI (voltage-input) poll rate, in seconds. When the configured
# rate matches this we omit the "@<poll>" edge suffix so the wire format stays
# byte-compatible with platform interfaces that predate the suffix.
_DEFAULT_VI_POLL = 0.4

# Hardware counters are unsigned 32-bit on some platforms (the ELPRO Quantum's
# free-running counter wraps rather than resetting). A *decrease* is therefore
# ambiguous - wrap, or the device rebooted and restarted from zero - so it is
# read as a wrap only when the previous value was in the top eighth of the
# range, which no realistic reboot lands in. Anything else re-baselines without
# inventing volume, because crediting 4 billion phantom pulses to a totaliser is
# far worse than losing the handful that straddled the reset.
_COUNTER_MODULUS = 2**32
_COUNTER_WRAP_THRESHOLD = _COUNTER_MODULUS - _COUNTER_MODULUS // 8


class FlowMeterApplication(Application):
    config_cls = FlowMeterConfig
    tags_cls = FlowMeterTags
    ui_cls = FlowMeterUI

    config: FlowMeterConfig
    tags: FlowMeterTags

    async def setup(self):
        cfg = self.config

        # Seconds represented by one "time base" (e.g. Per Hour -> 3600).
        self._seconds_per_base = TimeBase.SECONDS.get(cfg.rate_time_base.value, 3600.0)
        self._units = cfg.units.value
        self._rate_units = (
            f"{self._units}/{_TIME_BASE_ABBREV.get(cfg.rate_time_base.value, '')}"
        )

        # Drive the loop at the configured polling frequency.
        freq = cfg.poll_frequency.value
        self._loop_period = 1.0 / freq if freq and freq > 0 else 1.0
        self.loop_target_period = self._loop_period

        # Ceiling on the integration timestep. Guards the totaliser against a
        # wall-clock jump (e.g. NTP correcting a bad boot clock) being treated
        # as a huge slug of flow. Generous enough not to clip normal jitter.
        self._max_dt = max(self._loop_period * 10, 5.0)

        self._last_loop_time = None
        # Set properly by _setup_pulse_mode; defined here so analog and sim
        # modes have it too - _read_flow checks it on every cycle.
        self._use_hw_counter = False
        self._pulse_pin = None
        # Timestamp (epoch s) when flow first dropped to/below threshold; used to
        # close a session after the configured timeout. None while flowing.
        self._below_since = None

        # Resume the flow session across restarts from the persisted flag.
        initial = "flowing" if self.tags.flow_active.get() else "idle"
        self.session = FlowSessionState(initial)

        # Clear any fault carried over from a previous mode; analog reads
        # re-raise it on the first bad sample.
        await self.tags.sensor_fault_hidden.set(True)

        if cfg.sim_app_key.value:
            log.info("Simulation mode: reading flow from app %s", cfg.sim_app_key.value)
        elif cfg.mode.value == MeterMode.PULSE:
            await self._setup_pulse_mode()
        else:
            await self._setup_analog_mode()

    async def _setup_analog_mode(self):
        # Power up the transmitter if it's wired to a digital output.
        if self.config.power_pin.value is not None:
            await self.platform_iface.set_do(int(self.config.power_pin.value), True)

    def _resolve_pulse_source(self):
        """Resolve the configured pulse input to a (pin, edge, is_vi) triple.

        DI pins 4 and 5 on the Doovit aren't hardware digital-edge counters —
        they're *voltage-input* pulse counters mapped to analog inputs 0 and 1.
        For those we count on the AI pin (di_pin - 4) and encode the threshold in
        the edge string ("VI+10.0" for a rising step, "VI-10.0" for a falling
        one); the firmware polls the AI ~every 0.5s and emits a pulse whenever the
        sample-to-sample voltage step exceeds the threshold. Any other pin is a
        normal DI edge counter and keeps its plain "rising"/"falling" edge.
        """
        cfg = self.config
        pin = int(cfg.di_pin.value)
        edge = cfg.pulse_edge.value
        if pin in (4, 5):
            sign = "+" if edge == "rising" else "-"
            vi_edge = f"VI{sign}{cfg.vi_pulse_threshold.value}"
            # The firmware poll rate rides in the edge string as an optional
            # "@<seconds>" suffix. Only append it when it differs from the
            # firmware default, so the common case stays compatible with older
            # platform interfaces that parse the threshold without the suffix.
            poll = cfg.vi_poll_rate.value
            if poll and poll != _DEFAULT_VI_POLL:
                vi_edge = f"{vi_edge}@{poll}"
            return pin - 4, vi_edge, True
        return pin, edge, False

    async def _setup_pulse_mode(self):
        pin, edge, is_vi = self._resolve_pulse_source()
        if is_vi:
            log.info(
                "Pulse mode: voltage-input counter on AI pin %d (edge %s)", pin, edge
            )

        self._pulse_pin = pin
        self._use_hw_counter = await self._resolve_counter_source(pin, is_vi)

        # Rolling (timestamp, count) samples for the smoothed flow-rate window.
        self._pulse_samples = deque()
        self._prev_hw_count = None

        if self._use_hw_counter:
            # The device has been counting the whole time this app was down, so
            # there is nothing to recover from the event log - the first poll's
            # delta covers the outage on its own.
            self._prev_hw_count = self.tags.hw_pulse_count.get()
            self._prev_pulse_count = self.tags.pulse_count.get() or 0
            log.info("Pulse mode: polling the hardware counter on DI %d", pin)
            return

        await self._recover_missed_events(pin, edge, is_vi)

        # Per-loop pulse delta baseline (recovered pulses already land in the
        # totaliser via the derived total, so don't attribute them to a session).
        self._prev_pulse_count = self.tags.pulse_count.get() or 0

        # Seed the live counter so it continues the lifetime count, then listen.
        # For a VI source, `pin` is the AI pin and `edge` carries the threshold.
        log.info("Pulse mode: listening for live pulse events on DI %d", pin)
        self.platform_iface.start_di_pulse_listener(
            pin, self.on_pulse, edge, start_count=self._prev_pulse_count
        )

    async def _resolve_counter_source(self, pin, is_vi):
        """Decide whether to read a hardware counter or listen for live pulses."""
        source = self.config.pulse_source.value

        if is_vi:
            # A VI "pulse" is a voltage step the firmware detects by polling an
            # analog input, not a digital edge, so no hardware counter backs it
            # however the option is set.
            if source == PulseSource.COUNTER:
                log.warning(
                    "Pulse Source is set to %s, but DI pins 4-5 are voltage-input "
                    "counters with no hardware totaliser; using live events",
                    PulseSource.COUNTER,
                )
            return False

        if source == PulseSource.EVENTS:
            return False

        available = await self._hardware_counter_available(pin)
        if available:
            return True

        if source == PulseSource.COUNTER:
            # Explicitly asked for and not there. Fall back rather than report
            # no flow at all, but say so loudly - on a platform with no live
            # events either (an ELPRO Quantum) this app will now read nothing,
            # and the log is the only place that will explain why.
            log.error(
                "Pulse Source is set to %s but DI %d has no hardware counter; "
                "falling back to live pulse events",
                PulseSource.COUNTER,
                pin,
            )
        return False

    async def _hardware_counter_available(self, pin):
        """Probe the platform for a hardware counter on this pin.

        Asks the pin itself rather than reading the platform's advertised
        capabilities: what matters is whether a count comes back now, and a
        direct read also works against platform interfaces whose capability
        metadata predates these fields.
        """
        try:
            readings = await self.platform_iface.fetch_di_readings(pin)
        except AttributeError:
            # pydoover predates fetch_di_readings.
            log.info("Platform interface has no pulse-count support; using events")
            return False
        except Exception as e:  # noqa: BLE001 - probing must never break setup
            log.warning("Could not probe DI %d for a hardware counter: %s", pin, e)
            return False

        return bool(readings) and readings[0].pulse_count is not None

    async def _recover_missed_events(self, pin, edge, is_vi):
        """Best-effort replay of pulses missed while the app was down.

        Only for the live-events source: a hardware counter never stopped
        counting, so its first delta already covers the gap. Only when we have
        run before (last_pulse_dt set), so a fresh install does not replay the
        entire event history. VI pulses are not logged as DI events, so there
        is nothing to replay for them.
        """
        last_dt = self.tags.last_pulse_dt.get()
        if not last_dt or is_vi:
            return
        try:
            _synced, events = await self.platform_iface.fetch_di_events(
                pin, edge, events_from=int(last_dt * 1000)
            )
            if events:
                recovered = (self.tags.pulse_count.get() or 0) + len(events)
                await self.tags.pulse_count.set(recovered)
                log.info("Recovered %d pulse(s) missed while offline", len(events))
        except Exception as e:  # noqa: BLE001 - recovery is best effort
            log.warning("Could not recover missed pulses: %s", e)

    async def main_loop(self):
        now = time.time()
        dt = (now - self._last_loop_time) if self._last_loop_time else self._loop_period
        self._last_loop_time = now
        if dt <= 0:
            return  # clock went backwards (e.g. NTP step); skip this cycle
        if dt > self._max_dt:
            log.warning(
                "Clamping timestep %.1fs -> %.1fs (suspected clock jump)",
                dt,
                self._max_dt,
            )
            dt = self._max_dt

        reading = await self._read_flow(now, dt)
        if reading is None:
            return  # no data this cycle; hold previous values

        flow_rate, volume_delta, totaliser = reading
        await self.tags.flow_rate.set(flow_rate)
        await self.tags.totaliser.set(totaliser)

        await self._update_session(now, flow_rate, volume_delta)

    # --- Flow reading per mode ---------------------------------------------

    async def _read_flow(self, now, dt):
        """Return (flow_rate, volume_delta, totaliser) for this cycle, or None.

        ``flow_rate`` is in display units per time base; ``volume_delta`` and
        ``totaliser`` are in (calibrated) volume units.
        """
        cfg = self.config

        if cfg.sim_app_key.value:
            rate = self.get_tag("sim_flow_rate", cfg.sim_app_key.value)
            if rate is None:
                return None
            return self._integrate(max(0.0, float(rate)), dt)

        if cfg.mode.value == MeterMode.PULSE:
            if self._use_hw_counter:
                await self._poll_hardware_counter()
            return self._read_pulse(now, dt)

        return await self._read_analog(dt)

    async def _read_analog(self, dt):
        cfg = self.config
        raw = await self.platform_iface.fetch_ai(int(cfg.ai_pin.value))
        await self.tags.raw_signal.set(raw)

        if raw is None or raw < cfg.signal_min.value - cfg.signal_deadband.value:
            # Disconnected / faulted sensor: surface a warning and report zero
            # flow rather than holding a stale reading. The totaliser is left
            # untouched (no phantom volume accrues while the signal is bad).
            log.warning("Analog signal %s outside valid range; reporting no flow", raw)
            await self.tags.sensor_fault_hidden.set(False)
            return 0.0, 0.0, (self.tags.totaliser.get() or 0.0)

        await self.tags.sensor_fault_hidden.set(True)
        rate = self.scale_analog(
            raw,
            cfg.signal_min.value,
            cfg.signal_max.value,
            cfg.flow_at_signal_min.value,
            cfg.flow_at_signal_max.value,
        )
        return self._integrate(rate, dt)

    def _integrate(self, rate, dt):
        """Accumulate a flow *rate* into the totaliser (analog / sim modes)."""
        cal = self.config.totaliser_calibration.value
        volume_delta = (rate / self._seconds_per_base) * dt * cal
        totaliser = (self.tags.totaliser.get() or 0.0) + volume_delta
        return rate, volume_delta, totaliser

    def _read_pulse(self, now, dt):
        cfg = self.config
        count = self.tags.pulse_count.get() or 0
        offset = self.tags.pulse_offset.get() or 0
        k = cfg.k_factor.value or 1.0
        cal = cfg.totaliser_calibration.value

        # Per-loop delta feeds event-volume accumulation (the totaliser itself
        # is derived straight from the counter below, so it can't drift).
        d_pulses = count - self._prev_pulse_count
        self._prev_pulse_count = count
        volume_delta = (d_pulses / k) * cal

        # Smooth the displayed rate over a window of pulses. Dividing total
        # pulses by total elapsed window time avoids the 0/spike flicker you'd
        # get from a single loop's delta at low flow.
        self._pulse_samples.append((now, count))
        window = cfg.pulse_rate_window.value or self._loop_period
        while len(self._pulse_samples) > 2 and now - self._pulse_samples[0][0] > window:
            self._pulse_samples.popleft()
        t0, c0 = self._pulse_samples[0]
        elapsed = now - t0
        if elapsed > 0:
            rate = ((count - c0) / k / elapsed) * self._seconds_per_base * cal
        else:
            rate = 0.0

        totaliser = ((count - offset) / k) * cal
        return rate, volume_delta, totaliser

    @staticmethod
    def scale_analog(raw, signal_min, signal_max, flow_min, flow_max):
        """Linearly map a raw analog signal to a flow rate, clamped to the band."""
        if signal_max == signal_min:
            return 0.0
        frac = (raw - signal_min) / (signal_max - signal_min)
        frac = min(max(frac, 0.0), 1.0)
        return flow_min + frac * (flow_max - flow_min)

    # --- Hardware counter source --------------------------------------------

    async def _poll_hardware_counter(self):
        """Fold the device's counter into this app's lifetime pulse count.

        The device's value is not used as the count directly: it is the *device's*
        total, which may wrap, and which resets if the device reboots, whereas
        ``pulse_count`` has to stay monotonic because the totaliser and the
        totaliser-reset offset are both derived from it. So only the delta is
        carried across.
        """
        try:
            readings = await self.platform_iface.fetch_di_readings(self._pulse_pin)
        except Exception as e:  # noqa: BLE001 - a bad poll must not kill the loop
            log.warning("Could not read the pulse counter: %s", e)
            return

        raw = readings[0].pulse_count if readings else None
        if raw is None:
            # The counter went away mid-run (platform restarted into a state
            # without it). Hold the count; the next poll may bring it back.
            log.warning("DI %d reported no pulse count this cycle", self._pulse_pin)
            return

        raw = int(raw)
        delta = self._counter_delta(self._prev_hw_count, raw)
        self._prev_hw_count = raw
        await self.tags.hw_pulse_count.set(raw)

        if delta <= 0:
            return

        await self.tags.pulse_count.set((self.tags.pulse_count.get() or 0) + delta)
        await self.tags.last_pulse_dt.set(time.time())

    @staticmethod
    def _counter_delta(previous, current):
        """Pulses between two counter readings, or 0 if it restarted.

        ``previous`` is None on the very first poll of a fresh install, where
        there is no baseline to measure from and the device's lifetime total is
        emphatically not this meter's.
        """
        if previous is None:
            return 0
        if current >= previous:
            return current - previous
        if previous >= _COUNTER_WRAP_THRESHOLD:
            return (_COUNTER_MODULUS - previous) + current
        log.warning(
            "Pulse counter went backwards (%s -> %s); the device likely restarted. "
            "Re-baselining - pulses across the gap are lost rather than guessed",
            previous,
            current,
        )
        return 0

    # --- Pulse callback -----------------------------------------------------

    async def on_pulse(self, di, value, dt_secs, counter, edge):
        # ``counter`` continues the lifetime count (seeded with start_count).
        await self.tags.pulse_count.set(counter)
        await self.tags.last_pulse_dt.set(time.time())

    # --- Flow-session / event tracking -------------------------------------

    async def _update_session(self, now, flow_rate, volume_delta):
        cfg = self.config
        flowing = flow_rate is not None and flow_rate > cfg.event_flow_threshold.value

        if not self.session.flowing:
            if flowing:
                await self.session.start_flow()
                await self.tags.flow_active.set(True)
                await self.tags.event_started.set(int(now * 1000))
                await self.tags.event_volume.set(0.0)
                await self.tags.event_peak_flow.set(flow_rate)
                self._below_since = None
            return

        # Session active: accumulate volume and track the peak rate.
        await self.tags.event_volume.set(
            (self.tags.event_volume.get() or 0.0) + volume_delta
        )
        if flow_rate > (self.tags.event_peak_flow.get() or 0.0):
            await self.tags.event_peak_flow.set(flow_rate)

        if flowing:
            self._below_since = None
        else:
            if self._below_since is None:
                self._below_since = now
            elif now - self._below_since >= cfg.event_timeout.value * 60:
                await self._close_session(now)

    async def _close_session(self, now):
        started_ms = self.tags.event_started.get()
        volume = self.tags.event_volume.get() or 0.0
        peak = self.tags.event_peak_flow.get() or 0.0
        duration_s = (now - started_ms / 1000.0) if started_ms else 0.0

        summary = (
            f"{volume:.1f} {self._units} over {self._format_duration(duration_s)} "
            f"(peak {peak:.1f} {self._rate_units})"
        )
        log.info("Closing flow session: %s", summary)

        await self.session.stop_flow()
        await self.tags.flow_active.set(False)
        await self.tags.last_event_summary.set(summary)
        await self.send_notification(f"Flow event: {summary}")

        await self.tags.event_started.set(None)
        await self.tags.event_volume.set(0.0)
        await self.tags.event_peak_flow.set(0.0)
        self._below_since = None

    @staticmethod
    def _format_duration(seconds):
        seconds = int(seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    # --- UI handlers --------------------------------------------------------

    @ui.handler("reset_totaliser")
    async def on_reset_totaliser(self, ctx, value):
        log.info("Resetting totaliser")
        if (
            self.config.mode.value == MeterMode.PULSE
            and not self.config.sim_app_key.value
        ):
            await self.tags.pulse_offset.set(self.tags.pulse_count.get() or 0)
        await self.tags.totaliser.set(0.0)

    @ui.handler("reset_event")
    async def on_reset_event(self, ctx, value):
        log.info("Resetting current flow event")
        if self.session.flowing:
            await self.session.stop_flow()
        await self.tags.flow_active.set(False)
        await self.tags.event_started.set(None)
        await self.tags.event_volume.set(0.0)
        await self.tags.event_peak_flow.set(0.0)
        self._below_since = None
