"""Explicitly named simulated runner; never presents results as GPU measurements."""

from .local import LocalRunner


class SimulatedRunner(LocalRunner):
    pass
