import logging

from pydoover.state import StateMachine

log = logging.getLogger(__name__)


class FlowSessionState:
    """Tracks whether the meter is currently in a flow session.

    A "flow session" (event) is a continuous period where flow is above the
    configured threshold. The application decides *when* to fire the
    ``start_flow`` / ``stop_flow`` transitions (it owns the threshold +
    timeout logic and reads the clock); this machine just holds the state so
    the rest of the app can ask ``session.flowing`` cleanly.
    """

    state: str

    states = ["idle", "flowing"]

    transitions = [
        {"trigger": "start_flow", "source": "idle", "dest": "flowing"},
        {"trigger": "stop_flow", "source": "flowing", "dest": "idle"},
    ]

    def __init__(self, initial: str = "idle"):
        self.state_machine = StateMachine(
            states=self.states,
            transitions=self.transitions,
            model=self,
            initial=initial,
            # ignore start_flow while already flowing / stop_flow while idle
            ignore_invalid_triggers=True,
            queued=True,
        )

    @property
    def flowing(self) -> bool:
        return self.state == "flowing"

    async def on_enter_flowing(self):
        log.info("Flow session started")

    async def on_enter_idle(self):
        log.info("Flow session ended")
