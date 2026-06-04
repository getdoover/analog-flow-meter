from pathlib import Path

from pydoover import ui

from .app_config import TimeBase
from .app_tags import FlowMeterTags


# Time-base abbreviations used to build the flow-rate units string (e.g. "L/hr").
_TIME_BASE_ABBREV = {
    TimeBase.SECOND: "s",
    TimeBase.MINUTE: "min",
    TimeBase.HOUR: "hr",
    TimeBase.DAY: "day",
}


class FlowMeterUI(ui.UI):
    tabs = ui.TabContainer(
        "Tabs",
        name="tabs",
        children=[
            ui.Container(
                "Flow",
                name="flow_tab",
                children=[
                    ui.WarningIndicator(
                        "Sensor signal out of range",
                        name="sensor_fault",
                        hidden=FlowMeterTags.sensor_fault_hidden,
                        position=5,
                    ),
                    # NOTE: units / precision / ranges below are the defaults
                    # baked into the static schema. UI.setup() overrides them
                    # at runtime from config (units, time base, max flow).
                    ui.NumericVariable(
                        "Flow Rate",
                        value=FlowMeterTags.flow_rate,
                        name="flow_rate",
                        units="L/hr",
                        precision=1,
                        form=ui.Widget.radial,
                        ranges=[
                            ui.Range("Low", 0, 200, ui.Colour.blue),
                            ui.Range("Normal", 200, 800, ui.Colour.green),
                            ui.Range("High", 800, 1000, ui.Colour.yellow),
                        ],
                        position=10,
                    ),
                    ui.NumericVariable(
                        "Totaliser",
                        value=FlowMeterTags.totaliser,
                        name="totaliser",
                        units="L",
                        precision=0,
                        position=20,
                    ),
                    ui.Button("Reset Totaliser", name="reset_totaliser", position=30),
                ],
            ),
            ui.Container(
                "Events",
                name="events_tab",
                children=[
                    ui.BooleanVariable(
                        "Flow Active",
                        value=FlowMeterTags.flow_active,
                        name="flow_active",
                        position=10,
                    ),
                    ui.Timestamp(
                        "Current Event Started",
                        value=FlowMeterTags.event_started,
                        name="event_started",
                        position=20,
                    ),
                    ui.NumericVariable(
                        "Current Event Volume",
                        value=FlowMeterTags.event_volume,
                        name="event_volume",
                        units="L",
                        precision=0,
                        position=30,
                    ),
                    ui.NumericVariable(
                        "Current Event Peak Flow",
                        value=FlowMeterTags.event_peak_flow,
                        name="event_peak_flow",
                        units="L/hr",
                        precision=1,
                        position=40,
                    ),
                    ui.TextVariable(
                        "Last Event",
                        value=FlowMeterTags.last_event_summary,
                        name="last_event_summary",
                        position=50,
                    ),
                    ui.Button("Reset Current Event", name="reset_event", position=60),
                ],
            ),
        ],
    )

    async def setup(self):
        """Calibrate the gauge range and units from config at runtime."""
        units = self.config.units.value
        rate_units = (
            f"{units}/{_TIME_BASE_ABBREV.get(self.config.rate_time_base.value, '')}"
        )
        max_flow = float(self.config.max_flow.value or 0) or 1.0
        precision = int(self.config.flow_precision.value)
        tot_precision = int(self.config.totaliser_precision.value)

        flow = self._find("flow_rate")
        flow.units = rate_units
        flow.precision = precision
        flow.ranges = [
            ui.Range("Low", 0, 0.2 * max_flow, ui.Colour.blue),
            ui.Range("Normal", 0.2 * max_flow, 0.8 * max_flow, ui.Colour.green),
            ui.Range("High", 0.8 * max_flow, max_flow, ui.Colour.yellow),
        ]

        totaliser = self._find("totaliser")
        totaliser.units = units
        totaliser.precision = tot_precision

        event_volume = self._find("event_volume")
        event_volume.units = units
        event_volume.precision = tot_precision

        self._find("event_peak_flow").units = rate_units

    def _find(self, name: str):
        """Locate an element by name anywhere in the (instance) UI tree."""

        def walk(container):
            for child in getattr(container, "_children", {}).values():
                if child.name == name:
                    return child
                found = walk(child)
                if found is not None:
                    return found
            return None

        for element in self._elements.values():
            if element.name == name:
                return element
            found = walk(element)
            if found is not None:
                return found
        raise KeyError(name)


def export():
    FlowMeterUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "analog_flow_meter",
    )


if __name__ == "__main__":
    export()
