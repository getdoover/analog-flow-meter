import math
import time

from pydoover.docker import Application, run_app
from pydoover.tags import Tag, Tags


class SimulatorTags(Tags):
    # The flow-meter app reads this tag when a Simulator App Key is configured,
    # letting you exercise the app locally without real hardware.
    sim_flow_rate = Tag("number", default=0.0, live=True)


class FlowSimulator(Application):
    """Produces a believable flow signal that swings between zero and a peak.

    The value oscillates on a slow sine wave (in the app's configured flow
    units per time base) so you can watch the gauge move, the totaliser climb,
    and flow sessions open and close.
    """

    tags_cls = SimulatorTags

    PEAK_FLOW = 800.0  # units per time base at the top of the swing
    PERIOD_S = 300.0  # full low->high->low cycle length

    async def setup(self):
        self._started = time.time()

    async def main_loop(self):
        phase = (time.time() - self._started) / self.PERIOD_S * 2 * math.pi
        # sine shifted into [0, 1], scaled to the peak; floor small values to 0
        # so the meter spends part of each cycle genuinely "off".
        level = (math.sin(phase) + 1) / 2
        flow = self.PEAK_FLOW * max(0.0, level - 0.1) / 0.9
        await self.tags.sim_flow_rate.set(round(flow, 2))


def main():
    """Run the flow simulator application."""
    run_app(FlowSimulator())


if __name__ == "__main__":
    main()
