import logging
import time
from collections import deque

from pydoover.docker import Application
from pydoover import ui

from .app_config import FlowMeterConfig, MeterMode, TimeBase
from .app_tags import FlowMeterTags
from .app_ui import FlowMeterUI, _TIME_BASE_ABBREV
from .app_state import FlowSessionState

log = logging.getLogger(__name__)


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

    async def _setup_pulse_mode(self):
        cfg = self.config
        pin = int(cfg.di_pin.value)
        edge = cfg.pulse_edge.value

        # Recover pulses missed while the app was down (best effort). Only when
        # we've run before (last_pulse_dt set) so a fresh start doesn't replay
        # the entire event history.
        last_dt = self.tags.last_pulse_dt.get()
        if last_dt:
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

        # Per-loop pulse delta baseline (recovered pulses already land in the
        # totaliser via the derived total, so don't attribute them to a session).
        self._prev_pulse_count = self.tags.pulse_count.get() or 0

        # Rolling (timestamp, count) samples for the smoothed flow-rate window.
        self._pulse_samples = deque()

        # Seed the live counter so it continues the lifetime count, then listen.
        self.platform_iface.start_di_pulse_listener(
            pin, self.on_pulse, edge, start_count=self._prev_pulse_count
        )

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
