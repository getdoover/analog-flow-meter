from pydoover.docker import run_app

from .application import FlowMeterApplication


def main():
    """Run the application."""
    run_app(FlowMeterApplication())
