"""Built-in simulators."""

from __future__ import annotations

from ..registry import SimulatorPlugin


def builtin() -> list[SimulatorPlugin]:
    from .cement_kiln import PLUGIN as kiln
    from .water_tank import PLUGIN as tank

    return [kiln, tank]
